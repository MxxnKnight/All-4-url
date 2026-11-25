""" /live — record a live stream into Telegram, part by part.

A live stream has no end and no size, so none of the bot's normal
flows can take it: they all download a finished file, then send
it. A live recording is a different shape — ffmpeg reads the
stream continuously (stream-copy, no re-encode) and the segment
muxer cuts it into standalone MP4 parts, one per
LIVE_SEGMENT_MIN minutes. Each finished part is sent to Telegram
the moment it closes, then deleted from disk, so disk usage stays
at ~one part no matter how long the recording runs. The owner
stops it with the Stop button under the status message or under
the latest part — /cancel stops it too; a stream ending on its
own ends the recording the same way.

Two facts shape the design:
  * The segment muxer cuts by TIME only — there is no size
    option (verified against ffmpeg's own muxer help). At live
    bitrates a 15-minute part is a few hundred MB, far under
    Telegram's 2 GB cap, so the cap is never the binding
    constraint; the part length is the knob.
  * A recording survives nothing: the job lives only in this
    process (one recording at a time, LIVE_LOCK), so a Space
    restart or redeploy kills it mid-stream with no resume.
    That is accepted, not fixed — re-resolving the stream and
    restarting from live is what a retry does anyway.

The stream URL comes from yt-dlp (extract_info, download=False):
it knows live streams on 1000+ sites, including the cam sites
whose HLS playlists need API extraction. No streamlink, no
third-party plugins — yt-dlp is the engine. The one exception is
Stripchat, whose yt-dlp extractor refuses live free models (its
"private show" check trips on a stale page object); that site is
resolved by core/extractors/stripchat.py, which also decrypts the
Mouflon playlist through a loopback HLS proxy ffmpeg reads.
"""

import asyncio
import os
import re
import secrets
import time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from core import stats
from core.extractors import stripchat, ytdlp_engine
from core.extractors.direct import BROWSER_UA
from core.extractors.player_scan import HostError
from core.sender import send_files_routed
from core.utils import (ThrottledEditor, cleanup_dir, human_bytes,
                        human_time, make_job_dir)

#: One recording at a time — a second ffmpeg + upload stream would
#: fight the first for the Space's 2 CPUs and the outbound cap.
#: Deliberately NOT JOB_SEMAPHORE: a hours-long recording must not
#: hold a download slot for its whole life.
LIVE_LOCK = asyncio.Lock()

#: How often the watcher looks for finished parts, checks the Stop
#: button, and checks the bandwidth cap.
_POLL_SECONDS = 5
#: How long ffmpeg gets to finish its current part after SIGTERM
#: before it is killed outright.
_STOP_TIMEOUT = 10
#: A part smaller than this is not a real video (ffmpeg wrote a
#: header and nothing else) — skip it instead of sending a dud.
_MIN_PART_BYTES = 1024
#: Segment filename pattern — %03d keeps lexical order == time
#: order for up to 999 parts (~10 days at 15 minutes each).
_SEGMENT_PATTERN = "part%03d.mp4"
_SEGMENT_RE = re.compile(r"^part(\d{3})\.mp4$")
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


class LiveError(Exception):
    """Message safe to show the owner."""


class LiveJob:
    """One recording, from probe to final summary."""

    def __init__(self, job_id: str, client, chat_id: int, status,
                 user_id: int | None = None):
        self.job_id = job_id
        self.client = client
        self.chat_id = chat_id
        self.status = status
        self.user_id = user_id
        self.editor = ThrottledEditor(status)
        self.stop_requested = asyncio.Event()
        self.title = ""
        self.url = ""
        self.job_dir = ""
        self.log_handle = None
        self.proc = None
        self.started = 0.0
        self.parts_sent = 0
        self.bytes_sent = 0
        self.failed_parts = 0
        self.sent_names: set[str] = set()
        #: Per-recording arguments (v1.51.8, /live -t / -s): how
        #: long each part is, and an optional total time limit.
        #: Defaults are the config values / no limit.
        self.segment_min = config.LIVE_SEGMENT_MIN
        self.stop_after_min: int | None = None
        #: Epoch when the total limit runs out; set when the
        #: recording starts (0 = no limit).
        self.stop_deadline = 0.0
        #: The last part message sent, so the Stop button can ride on
        #: it (v1.51.7) and be cleared when the recording ends.
        self.last_part_msg = None

    def request_stop(self) -> None:
        """The Stop button: the watcher ends the recording cleanly."""
        self.stop_requested.set()


#: Live jobs by id, so the Stop button callback can find them.
ACTIVE: dict[str, LiveJob] = {}


def busy() -> bool:
    return LIVE_LOCK.locked()


def get_job(job_id: str) -> LiveJob | None:
    return ACTIVE.get(job_id)


def active_job() -> LiveJob | None:
    """The recording running right now, if any (at most one — the
    slot enforces it). /cancel uses this instead of a job id."""
    return next(iter(ACTIVE.values()), None)


async def acquire_slot() -> bool:
    """Atomically claim the one recording slot; False if taken.

    asyncio.Lock.acquire() returns immediately (no task switch)
    when the lock is free, so the check + acquire here cannot be
    interleaved by a second /live landing in between.
    """
    if LIVE_LOCK.locked():
        return False
    await LIVE_LOCK.acquire()
    return True


def release_slot() -> None:
    try:
        LIVE_LOCK.release()
    except RuntimeError:
        pass  # already released — defensive only


def _unwrap(info: dict) -> dict | None:
    """A playlist result -> the live entry (or the first entry)."""
    entries = info.get("entries")
    if entries:
        for entry in entries:
            if isinstance(entry, dict) and entry.get("is_live"):
                return entry
        first = entries[0]
        return first if isinstance(first, dict) else None
    return info


def _headers_of(fmt: dict) -> dict:
    """Headers ffmpeg needs: Referer + User-Agent only. Enough for
    the hosts that check them; cookies stay out (live HLS URLs are
    tokenised in the URL itself, and a cookie on a subprocess
    command line would show up in `ps`)."""
    headers = fmt.get("http_headers") or {}
    out = {}
    for key in ("Referer", "User-Agent"):
        value = headers.get(key)
        if value:
            out[key] = value
    return out


def _pick_stream(info: dict) -> tuple[list[str], dict]:
    """Best playable stream URL(s) + HTTP headers for ffmpeg.

    One combined (video+audio) format when the site offers it;
    otherwise the best video-only + best audio-only pair, which
    ffmpeg reads as two inputs. Empty list when nothing playable
    exists.
    """
    formats = [f for f in (info.get("formats") or [])
               if isinstance(f, dict) and f.get("url")]

    def _score(fmt):
        return (fmt.get("height") or 0, fmt.get("tbr") or 0)

    combined = [f for f in formats
                if f.get("vcodec") not in (None, "none")
                and f.get("acodec") not in (None, "none")]
    if combined:
        best = max(combined, key=_score)
        return [best["url"]], _headers_of(best)
    video = [f for f in formats
             if f.get("vcodec") not in (None, "none")
             and f.get("acodec") in (None, "none")]
    audio = [f for f in formats
             if f.get("acodec") not in (None, "none")
             and f.get("vcodec") in (None, "none")]
    if video and audio:
        v = max(video, key=_score)
        a = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        return [v["url"], a["url"]], _headers_of(v)
    if video:
        best = max(video, key=_score)
        return [best["url"]], _headers_of(best)
    if audio:
        best = max(audio,
                   key=lambda f: f.get("abr") or f.get("tbr") or 0)
        return [best["url"]], _headers_of(best)
    url = info.get("url") or ""
    if url.startswith("http"):
        return [url], _headers_of(info)
    return [], {}


async def probe_live(url: str) -> dict:
    """{title, stream_urls, headers} for a live stream, or LiveError.

    Refuses anything yt-dlp cannot read, anything that is not live
    right now, and live streams with no playable format.
    """
    # Stripchat is resolved by its own extractor, not yt-dlp: yt-dlp
    # reports every model whose page still carries the previous show's
    # object as "in a private show", so free live models were refused
    # as paid (see core/extractors/stripchat.py).
    if stripchat.is_site_url(url):
        try:
            return await stripchat.open_stream(url)
        except HostError as exc:
            raise LiveError(str(exc)) from exc
    try:
        info = await ytdlp_engine.extract_info(url)
    except ytdlp_engine.YtdlpError as exc:
        raise LiveError(f"I couldn't read that link ({exc})")
    info = _unwrap(info)
    if info is None:
        raise LiveError("that link didn't turn into a video I can read")
    live_status = (info.get("live_status") or "").lower()
    if live_status == "is_upcoming":
        raise LiveError(
            "that stream hasn't started yet — try again once it's live")
    if live_status == "was_live":
        raise LiveError(
            "that stream already ended — there's nothing live to record")
    if not info.get("is_live") and live_status != "is_live":
        raise LiveError(
            "that doesn't look like a live stream — /live only records "
            "streams that are live right now")
    stream_urls, headers = _pick_stream(info)
    if not stream_urls:
        raise LiveError(
            "that stream is live, but I couldn't find a playable "
            "stream URL for it")
    title = (info.get("title") or "").strip() or "Live stream"
    return {"title": title, "stream_urls": stream_urls,
            "headers": headers}


def ffmpeg_cmd(stream_urls: list[str], headers: dict,
               out_pattern: str, segment_seconds: int | None = None) -> list[str]:
    """ffmpeg: live stream -> timed MP4 segments, stream-copy.

    -nostdin: ffmpeg must never read the terminal (it would take
    'q' as quit). -reconnect: a dropped segment fetch retries
    instead of killing a hours-long recording. -segment_time cuts
    at the first keyframe AFTER the interval, so parts run a few
    seconds long; -reset_timestamps makes each part start at 0, so
    every part plays standalone. Input options repeat before each
    -i: they apply to the following input only.
    `segment_seconds` overrides the config default (v1.51.8:
    /live -t sets it per recording).
    """
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    header_str = "".join(f"{k}: {v}\r\n"
                         for k, v in headers.items()) if headers else ""
    for stream_url in stream_urls:
        cmd += ["-user_agent", BROWSER_UA,
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5"]
        if header_str:
            cmd += ["-headers", header_str]
        cmd += ["-i", stream_url]
    cmd += ["-map", "0:v:0?",
            "-map", f"{len(stream_urls) - 1}:a:0?",
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(segment_seconds
                                if segment_seconds is not None
                                else config.LIVE_SEGMENT_SECONDS),
            "-reset_timestamps", "1",
            "-segment_format", "mp4",
            out_pattern]
    return cmd


async def _spawn_ffmpeg(cmd, log_handle):
    """One indirection so tests can swap in a fake subprocess."""
    return await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=log_handle)


def _segments(job_dir: str) -> list[str]:
    """Segment files in time order (part000, part001, ...)."""
    try:
        names = os.listdir(job_dir)
    except OSError:
        return []
    names = [n for n in names if _SEGMENT_RE.match(n)]
    names.sort()
    return [os.path.join(job_dir, n) for n in names]


def _ready_segments(job_dir: str, sent: set, recorder_done: bool) -> list[str]:
    """Complete, unsent segments in order.

    A segment is complete ONLY when a newer segment exists (ffmpeg
    has moved on) or the recorder has exited — never the segment
    ffmpeg is still writing.
    """
    found = _segments(job_dir)
    if not found:
        return []
    if recorder_done:
        complete = found
    else:
        complete = found[:-1]  # the last one is still being written
    return [p for p in complete if p not in sent]


def _status_text(job: LiveJob, prefix: str) -> str:
    elapsed = human_time(time.time() - job.started) if job.started else "0:00"
    stop_note = (f" · stops after {job.stop_after_min} min"
                 if job.stop_after_min else "")
    return (f"{prefix} — {job.title}\n"
            f"⏱ {elapsed} · 📦 {job.parts_sent} part(s) sent · "
            f"{human_bytes(job.bytes_sent)}{stop_note}\n"
            f"Parts arrive as they finish "
            f"({job.segment_min} min each). "
            f"Tap ⏹ Stop to end the recording.")


def _stop_markup(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏹ Stop recording",
                             callback_data=f"lv:stop:{job_id}")]])


async def _send_segment(job: LiveJob, path: str) -> None:
    """Send one finished part, then delete it from disk.

    A send failure is recorded, not fatal — the next part still
    goes out, and the final summary names the failed ones.
    Uploaded bytes are recorded by sender.send_files itself;
    only the downloaded bytes are recorded here.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    job.sent_names.add(path)
    if size < _MIN_PART_BYTES:
        # ffmpeg wrote a header and nothing else (e.g. the stream
        # died the moment this part opened) — not a video.
        try:
            os.remove(path)
        except OSError:
            pass
        return
    part_no = job.parts_sent + 1
    elapsed = human_time(time.time() - job.started)
    caption = (f"🔴 {job.title} — Part {part_no}\n"
               f"⏱ {elapsed} into the recording")
    try:
        stats.record("downloaded", size)
        sent = await send_files_routed(job.client, job.chat_id, [path],
                                       "uploader", caption=caption,
                                       status_msg=job.status,
                                       user_id=job.user_id,
                                       force_document=False)  # live parts
        # are always videos — they are meant to be watched as they
        # arrive, whatever the URL-upload "Send as" setting says.
        if sent:
            # The Stop button rides on the part itself (v1.51.7):
            # the status message scrolls away under the videos, so
            # the newest part is where the owner is looking. A part
            # sent to Saved Messages isn't the bot's to edit — the
            # attach just fails quietly there.
            job.last_part_msg = sent[-1]
            try:
                await job.last_part_msg.edit_reply_markup(
                    _stop_markup(job.job_id))
            except Exception:
                pass
        job.parts_sent += 1
        job.bytes_sent += size
    except Exception:
        job.failed_parts += 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _terminate(job: LiveJob) -> None:
    """End ffmpeg gracefully: SIGTERM lets it write the current
    part's trailer; SIGKILL only if it refuses to die."""
    proc = job.proc
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except Exception:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass


def _ffmpeg_tail(job: LiveJob) -> str:
    """Last ffmpeg error line, URLs stripped (they may be signed)."""
    if not job.job_dir:
        return ""
    try:
        with open(os.path.join(job.job_dir, "ffmpeg.log"),
                  encoding="utf-8", errors="replace") as handle:
            text = handle.read()[-2000:]
    except OSError:
        return ""
    text = _URL_RE.sub("[source]", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


async def _watch(job: LiveJob) -> str:
    """Poll until the recording must end. Returns the end reason:
    "stopped" | "ended" | "bandwidth" | "failed" | "timeup"."""
    proc = job.proc
    while True:
        if job.stop_requested.is_set():
            return "stopped"
        if (job.stop_deadline
                and time.time() >= job.stop_deadline):
            # /live -s: the total time ran out — end it the same
            # clean way as a Stop tap (ffmpeg gets SIGTERM, the
            # partial last part is finished and sent by _finish).
            return "timeup"
        if stats.bandwidth_blocked():
            return "bandwidth"
        done = proc.returncode is not None
        for path in _ready_segments(job.job_dir, job.sent_names, done):
            await _send_segment(job, path)
        if done:
            return "ended" if proc.returncode == 0 else "failed"
        await job.editor.edit(_status_text(job, "🔴 Recording"))
        await asyncio.sleep(_POLL_SECONDS)


async def _finish(job: LiveJob, reason: str) -> None:
    """Stop ffmpeg, send what's left, write the final summary.

    reason: "stopped" (Stop button) | "ended" (stream over) |
    "bandwidth" (monthly cap) | "failed" (ffmpeg died) |
    "timeup" (/live -s total time ran out).
    """
    if reason in ("stopped", "bandwidth", "timeup"):
        await _terminate(job)
    # ffmpeg is gone now (or died on its own) — every remaining
    # segment is complete, including the partial last one.
    for path in _ready_segments(job.job_dir, job.sent_names, True):
        await _send_segment(job, path)
    try:
        await job.status.edit_reply_markup(None)
    except Exception:
        pass
    if job.last_part_msg is not None:
        # The Stop button on the last part is dead now — clear it
        # so a later tap doesn't promise a stop.
        try:
            await job.last_part_msg.edit_reply_markup(None)
        except Exception:
            pass
    if reason == "failed" and job.parts_sent > 0:
        head = "⚠️ Recording ended early — the stream died"
    else:
        head = {
            "stopped": "⏹ Recording stopped",
            "ended": "✅ Stream ended — recording complete",
            "bandwidth": ("⛔ Recording stopped — the monthly "
                          "bandwidth cap was reached"),
            "timeup": "⏱ Time limit reached — recording stopped",
            "failed": "❌ Recording failed",
        }[reason]
    elapsed = human_time(time.time() - job.started) if job.started else "0:00"
    lines = [f"{head} — {job.title}",
             f"⏱ {elapsed} · 📦 {job.parts_sent} part(s) sent · "
             f"{human_bytes(job.bytes_sent)}"]
    if job.failed_parts:
        lines.append(f"⚠️ {job.failed_parts} part(s) couldn't be sent.")
    if reason == "failed":
        tail = _ffmpeg_tail(job)
        if tail:
            lines.append(f"ffmpeg said: {tail}")
        if job.parts_sent == 0:
            lines.append("Nothing was recorded — the stream may have "
                         "died or blocked the bot. Try again, or check "
                         "the link plays in your browser.")
    await job.editor.edit("\n".join(lines), force=True)
    stats.record_job("live",
                     ok=not (reason == "failed" and job.parts_sent == 0))


async def run_live_job(client, message, url: str, status,
                       segment_min: int | None = None,
                       stop_after_min: int | None = None) -> None:
    """Record `url` until Stop / stream end / bandwidth cap / -s limit.

    The caller (plugins/live.py) already claimed the slot with
    acquire_slot(); this releases it in `finally`, and cleans
    the job dir up either way. `segment_min` overrides the
    config part length for this recording (/live -t);
    `stop_after_min` ends it after that many minutes (/live -s).
    """
    job_id = secrets.token_hex(4)
    job = LiveJob(job_id, client, message.chat.id, status,
                  message.from_user.id if message.from_user else None)
    job.url = url
    if segment_min is not None:
        job.segment_min = segment_min
    job.stop_after_min = stop_after_min
    recorded = False
    try:
        await job.editor.edit("🔍 Checking the link…", force=True)
        try:
            probed = await probe_live(url)
        except LiveError as exc:
            await job.editor.edit(f"❌ {exc}", force=True)
            stats.record_job("live", ok=False)
            recorded = True
            return
        job.title = probed["title"]
        job.job_dir = make_job_dir()
        ACTIVE[job_id] = job
        job.started = time.time()
        if job.stop_after_min:
            job.stop_deadline = (job.started
                                 + job.stop_after_min * 60)
        pattern = os.path.join(job.job_dir, _SEGMENT_PATTERN)
        cmd = ffmpeg_cmd(probed["stream_urls"], probed["headers"],
                         pattern,
                         segment_seconds=job.segment_min * 60)
        # ffmpeg's stderr goes to a file, not a pipe: a pipe nobody
        # drains would fill up and block ffmpeg mid-recording, and
        # -v error keeps the file to error lines only.
        job.log_handle = open(os.path.join(job.job_dir, "ffmpeg.log"),
                              "ab")
        job.proc = await _spawn_ffmpeg(cmd, job.log_handle)
        try:
            await status.edit_reply_markup(_stop_markup(job_id))
        except Exception:
            pass
        await job.editor.edit(_status_text(job, "🔴 Recording"),
                              force=True)
        reason = await _watch(job)
        await _finish(job, reason)
        recorded = True
    except asyncio.CancelledError:
        # Shutdown mid-recording: kill ffmpeg synchronously (no
        # awaits — the task is already being torn down) and let
        # the finally block clean the job dir up.
        try:
            if job.proc is not None and job.proc.returncode is None:
                job.proc.terminate()
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            await _terminate(job)
            await job.editor.edit(
                f"❌ Recording failed unexpectedly: {exc}", force=True)
        except Exception:
            pass
        if not recorded:
            stats.record_job("live", ok=False)
    finally:
        ACTIVE.pop(job_id, None)
        try:
            if job.log_handle is not None:
                job.log_handle.close()
        except Exception:
            pass
        if job.job_dir:
            cleanup_dir(job.job_dir)
        release_slot()
