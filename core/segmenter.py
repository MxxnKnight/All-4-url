"""Split downloads (/sdl): watchable segments of an oversized video.

A direct-link video too big for Telegram's 2 GB cap is cut into
complete, playable parts — each uploaded as a real video — instead
of being refused or byte-split into unplayable pieces.

How it dodges the disk wall: ffmpeg reads the REMOTE file directly,
seeking with HTTP range requests, and writes only the current part.
The whole file never exists locally; at most one part (≤ the size
cap) is on disk at any moment, and free space is re-checked before
every part.

Every part obeys BOTH caps — max duration and max bytes (config
SEGMENT_MAX_MIN / SEGMENT_MAX_MB). The planner estimates part
lengths from the probed average bitrate; after each cut the part's
REAL size and duration are measured and the working bitrate is
recomputed from what has actually been cut, so later parts
self-correct on variable-bitrate files. A part that still lands
over the byte cap is re-cut shorter before uploading.

Cuts are stream-copy (-c copy) at keyframes: fast, no re-encode,
but a boundary can sit a few seconds off the planned time. All
audio/subtitle tracks are kept (-map 0).

Only ONE segment job runs at a time (SEGMENT_LOCK) — these jobs are
long and disk-bound; the /sdl callback refuses a second start.
"""

import asyncio
import json
import os
import shutil
import subprocess

import aiohttp

import config
from core import stats
from core.extractors.direct import BROWSER_UA
from core.sender import send_files_routed
from core.utils import (JOB_SEMAPHORE, cleanup_dir, human_bytes,
                        make_job_dir, run_blocking)

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts",
              ".mpg", ".mpeg", ".wmv", ".flv"}

#: Keep this much headroom under the byte cap when *planning* a part
#: (the measure-and-recut loop handles the rest).
_PLAN_HEADROOM = 0.92
#: A part shorter than this is merged into the previous one instead —
#: nobody wants a 12-second "Part 9".
_MIN_PART_SECONDS = 20
#: Free disk required beyond one full part before a cut may start.
_DISK_MARGIN = 250 * 1000 * 1000

#: Held for the whole run of a segment job — one at a time, ever.
SEGMENT_LOCK = asyncio.Lock()


class SegmentError(Exception):
    """User-facing segment failure."""


def busy() -> bool:
    return SEGMENT_LOCK.locked()


def looks_like_video(info: dict) -> bool:
    mime = (info.get("mime") or "").lower()
    if mime.startswith("video/"):
        return True
    ext = os.path.splitext(info.get("filename") or "")[1].lower()
    return ext in VIDEO_EXTS


def fmt_time(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def caps() -> tuple[int, int]:
    """(max_seconds_per_part, max_bytes_per_part) from config."""
    return config.SEGMENT_MAX_MIN * 60, config.SEGMENT_MAX_MB * 1000 * 1000


def plan_part_duration(remaining: float, bitrate: float,
                       max_seconds: float, max_bytes: int) -> float:
    """One part's planned length under BOTH caps.

    bitrate is bits/second (average); unknown bitrate → time cap."""
    duration = min(remaining, max_seconds)
    if bitrate > 0:
        by_size = (max_bytes * _PLAN_HEADROOM) / (bitrate / 8.0)
        duration = min(duration, by_size)
    return max(duration, 1.0)


def estimate_plan(duration: float, bitrate: float,
                  max_seconds: float, max_bytes: int) -> list[dict]:
    """[{start, duration, est_bytes}] for the whole video — the
    /sdl report card's plan. Mirrors the runner's planner, including
    absorbing a stub tail into the last part."""
    plan = []
    start = 0.0
    while start < duration - 1:
        remaining = duration - start
        dur = plan_part_duration(remaining, bitrate,
                                 max_seconds, max_bytes)
        if remaining - dur < _MIN_PART_SECONDS:
            dur = remaining
        plan.append({"start": start, "duration": dur,
                     "est_bytes": int(dur * bitrate / 8.0)
                     if bitrate > 0 else 0})
        start += dur
    return plan


async def check_range_support(url: str) -> bool:
    """True when the host answers a 1-byte Range request with 206 —
    i.e. ffmpeg can seek instead of reading from byte zero."""
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                    url, headers={"User-Agent": BROWSER_UA,
                                  "Range": "bytes=0-0"},
                    allow_redirects=True) as resp:
                if resp.status != 206:
                    return False
                await resp.content.read(1)
                return True  # a 206 answer is range support
    except Exception:
        return False


def _ffprobe_json(target: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams",
         "-user_agent", BROWSER_UA, target],
        capture_output=True, text=True, timeout=90)
    return json.loads(out.stdout or "{}")


def probe_remote(url: str) -> dict | None:
    """{duration, size, bitrate, width, height} for a remote video.

    None when ffprobe can't read it or there's no video stream.
    ffprobe seeks with range requests — the file is not downloaded."""
    try:
        data = _ffprobe_json(url)
    except Exception:
        return None
    video = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration") or 0)
        size = int(fmt.get("size") or 0)
        bitrate = float(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    if bitrate <= 0 and size > 0:
        bitrate = size * 8.0 / duration
    return {"duration": duration, "size": size, "bitrate": bitrate,
            "width": video.get("width") or 0,
            "height": video.get("height") or 0}


def _duration_of(path: str) -> float:
    try:
        data = _ffprobe_json(path)
        return float(data.get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


def _cut(url: str, start: float, duration: float, out_path: str):
    """Stream-copy [start, start+duration) of the remote file to
    out_path. Returns (actual_duration, actual_size)."""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-user_agent", BROWSER_UA,
           "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "5",
           "-ss", f"{start:.3f}", "-i", url,
           "-t", f"{duration:.3f}",
           "-map", "0", "-c", "copy",
           "-avoid_negative_ts", "make_zero",
           out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=3600)
    if proc.returncode != 0 or not os.path.isfile(out_path) \
            or os.path.getsize(out_path) == 0:
        tail = (proc.stderr or "").strip().splitlines()
        raise SegmentError("ffmpeg could not cut that part"
                           + (f": {tail[-1]}" if tail else ""))
    return _duration_of(out_path) or duration, os.path.getsize(out_path)


async def run_segment_job(client, message, url: str, info: dict,
                          probed: dict, status, editor) -> dict:
    """Cut + upload the whole video, part by part.

    Caller has already probed (the /sdl card). Returns
    {"parts": n, "lines": [summary lines]}. Raises SegmentError
    with a user-facing message."""
    async with SEGMENT_LOCK, JOB_SEMAPHORE:
        max_seconds, max_bytes = caps()
        total_dur = probed["duration"]
        total_size = info.get("size") or probed["size"]
        name = info.get("filename") or "video"
        ext = os.path.splitext(name)[1].lower()
        if ext not in VIDEO_EXTS:
            ext = ".mkv"
        job_dir = make_job_dir()
        lines: list[str] = []
        part_no = 0
        start = 0.0
        bitrate = probed["bitrate"]
        cut_bytes = 0
        cut_seconds = 0.0
        try:
            while start < total_dur - 1:
                if part_no and stats.bandwidth_blocked():
                    lines.append("⛔ Stopped early: the monthly "
                                 "bandwidth cap was reached — the "
                                 "remaining parts were not sent.")
                    break
                free = shutil.disk_usage(job_dir).free
                if free < max_bytes + _DISK_MARGIN:
                    raise SegmentError(
                        f"Not enough free disk for the next part "
                        f"({human_bytes(free)} free, need about "
                        f"{human_bytes(max_bytes + _DISK_MARGIN)}).")
                remaining = total_dur - start
                duration = plan_part_duration(remaining, bitrate,
                                              max_seconds, max_bytes)
                if remaining - duration < _MIN_PART_SECONDS:
                    duration = remaining  # absorb the stub tail
                part_no += 1
                out_path = os.path.join(job_dir,
                                        f"part{part_no:02d}{ext}")
                await editor.edit(
                    f"✂️ Cutting part {part_no} "
                    f"({fmt_time(start)}–"
                    f"{fmt_time(start + duration)})…", force=True)
                actual_dur, actual_size = await run_blocking(
                    _cut, url, start, duration, out_path)
                attempts = 0
                while actual_size > max_bytes and attempts < 3:
                    attempts += 1
                    duration *= (max_bytes / actual_size) * 0.95
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    await editor.edit(
                        f"✂️ Part {part_no} came out at "
                        f"{human_bytes(actual_size)} — over the "
                        "size cap, re-cutting it shorter…",
                        force=True)
                    actual_dur, actual_size = await run_blocking(
                        _cut, url, start, duration, out_path)
                if actual_size > max_bytes:
                    raise SegmentError(
                        f"Part {part_no} wouldn't fit under "
                        f"{human_bytes(max_bytes)} even after "
                        "re-cutting — extremely spiky bitrate.")
                stats.record("downloaded", actual_size)
                end = start + actual_dur
                caption = (f"🎬 {name} — Part {part_no}\n"
                           f"⏱ {fmt_time(start)}–{fmt_time(end)} · "
                           f"{human_bytes(actual_size)}")
                await editor.edit(
                    f"⬆️ Uploading part {part_no} "
                    f"({human_bytes(actual_size)}, "
                    f"{fmt_time(actual_dur)})…", force=True)
                await send_files_routed(client, message.chat.id,
                                        [out_path], "uploader",
                                        caption=caption,
                                        status_msg=status)
                lines.append(f"• Part {part_no}: "
                             f"{fmt_time(start)}–{fmt_time(end)} · "
                             f"{human_bytes(actual_size)}")
                cut_bytes += actual_size
                cut_seconds += actual_dur
                if cut_seconds > 0:
                    # Self-correct the working bitrate from reality.
                    bitrate = cut_bytes * 8.0 / cut_seconds
                start = end
                try:
                    os.remove(out_path)
                except OSError:
                    pass
                if actual_dur <= 0:
                    break
        finally:
            cleanup_dir(job_dir)
        if part_no == 0:
            raise SegmentError("No parts could be cut from that video.")
        return {"parts": part_no, "lines": lines}
