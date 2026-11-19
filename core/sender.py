"""Uploading finished files back to Telegram, with progress + thumbnails."""

import asyncio
import json
import os
import time

from pyrogram import StopTransmission
from pyrogram.errors import FloodPremiumWait, FloodWait, SlowmodeWait
from pyrogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                            InputMediaPhoto)

from core import splitter, stats, tmdb
from core.filename_parser import clean_display, parse_filename
from core.jobs import JobCancelled
from core.splitter import is_split_part
from core.utils import (ThrottledEditor, guess_kind, human_bytes,
                        progress_text, run_blocking)


#: ISO 639 language codes (2- and 3-letter) to display names, for
#: the audio/subtitle languages in upload captions (v1.44.0).
#: ffprobe usually reports 3-letter codes in stream tags.
_LANG_NAMES = {
    "en": "English", "eng": "English",
    "hi": "Hindi", "hin": "Hindi",
    "ml": "Malayalam", "mal": "Malayalam",
    "ta": "Tamil", "tam": "Tamil",
    "te": "Telugu", "tel": "Telugu",
    "kn": "Kannada", "kan": "Kannada",
    "mr": "Marathi", "mar": "Marathi",
    "bn": "Bengali", "ben": "Bengali",
    "pa": "Punjabi", "pan": "Punjabi",
    "gu": "Gujarati", "guj": "Gujarati",
    "or": "Odia", "ori": "Odia",
    "as": "Assamese", "asm": "Assamese",
    "ur": "Urdu", "urd": "Urdu",
    "es": "Spanish", "spa": "Spanish",
    "fr": "French", "fra": "French", "fre": "French",
    "de": "German", "deu": "German", "ger": "German",
    "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese",
    "ru": "Russian", "rus": "Russian",
    "ja": "Japanese", "jpn": "Japanese",
    "ko": "Korean", "kor": "Korean",
    "zh": "Chinese", "zho": "Chinese", "chi": "Chinese",
    "ar": "Arabic", "ara": "Arabic",
    "nl": "Dutch", "nld": "Dutch", "dut": "Dutch",
    "sv": "Swedish", "swe": "Swedish",
    "tr": "Turkish", "tur": "Turkish",
    "id": "Indonesian", "ind": "Indonesian",
    "ms": "Malay", "msa": "Malay", "may": "Malay",
    "th": "Thai", "tha": "Thai",
    "vi": "Vietnamese", "vie": "Vietnamese",
    "pl": "Polish", "pol": "Polish",
    "uk": "Ukrainian", "ukr": "Ukrainian",
}

#: Video codecs that are still images, not video — cover art
#: embedded in audio files (v1.44.0). An mp3 with a cover must
#: not be sent as a video.
_IMAGE_CODECS = {"mjpeg", "png", "bmp", "gif", "webp", "tiff"}


def _lang_name(code: str) -> str | None:
    """'mal' -> 'Malayalam'; None for unknown/undetermined codes."""
    if not code:
        return None
    code = code.strip().lower()
    if code in {"und", "unknown", "none", "zxx", "mis"}:
        return None
    return _LANG_NAMES.get(code, code.upper() if len(code) <= 3 else code)


def _quality_bucket(height: int) -> str:
    """1080 -> '1080p' (the label moon reads at a glance)."""
    height = int(height or 0)
    if height <= 0:
        return ""
    for cap in (480, 540, 720, 1080, 2160, 4320):
        if height <= cap:
            return f"{cap}p"
    return f"{height}p"


def _ffprobe(path: str) -> dict:
    """Best-effort media facts for a file ({} when unavailable).

    Keys: width/height/duration (video only), has_video (a REAL
    video stream — image codecs like mjpeg cover art don't count),
    has_audio, audio_langs/sub_langs (display names, de-duplicated,
    in stream order), probed (True when ffprobe actually ran).
    """
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(out.stdout or "{}")
        info: dict = {"probed": True}
        audio_langs: list[str] = []
        sub_langs: list[str] = []
        for stream in data.get("streams", []):
            ctype = stream.get("codec_type", "")
            if ctype == "video":
                codec = (stream.get("codec_name") or "").lower()
                if codec in _IMAGE_CODECS:
                    continue  # cover art, not a video stream
                if "width" not in info:
                    info["width"] = stream.get("width") or 0
                    info["height"] = stream.get("height") or 0
                info["has_video"] = True
            elif ctype == "audio":
                info["has_audio"] = True
                name = _lang_name(
                    (stream.get("tags") or {}).get("language", ""))
                if name and name not in audio_langs:
                    audio_langs.append(name)
            elif ctype == "subtitle":
                name = _lang_name(
                    (stream.get("tags") or {}).get("language", ""))
                if name and name not in sub_langs:
                    sub_langs.append(name)
        if audio_langs:
            info["audio_langs"] = audio_langs
        if sub_langs:
            info["sub_langs"] = sub_langs
        duration = float(data.get("format", {}).get("duration") or 0)
        if duration:
            info["duration"] = int(duration)
        return info
    except Exception:
        return {}


async def _poster_thumb(video_path: str) -> str | None:
    """A TMDB poster as the video thumbnail (v1.44.0).

    Needs a TMDB token configured (else None immediately, no
    network) and a parseable title from the filename. The poster
    bytes are written next to the video; the caller deletes the
    file after sending, like a frame thumbnail. Any failure
    returns None and the frame thumbnail is used instead.
    """
    parsed = parse_filename(os.path.basename(video_path))
    title = parsed.get("title") or ""
    if not title:
        return None
    try:
        data = await tmdb.get_poster(title, parsed.get("year"))
    except Exception:
        return None
    if not data:
        return None
    thumb = video_path + ".poster.jpg"
    try:
        with open(thumb, "wb") as fh:
            fh.write(data)
        return thumb if os.path.getsize(thumb) > 0 else None
    except OSError:
        return None


def _make_thumb(video_path: str) -> str | None:
    """Grab a frame at ~1s as a JPEG thumbnail, if ffmpeg is around."""
    import subprocess

    thumb = video_path + ".thumb.jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", video_path,
             "-frames:v", "1", "-q:v", "4", thumb],
            capture_output=True, timeout=30,
        )
        return thumb if os.path.isfile(thumb) and os.path.getsize(thumb) > 0 else None
    except Exception:
        return None


def _progress_callback(editor: ThrottledEditor, action: str):
    started = time.time()

    async def callback(current: int, total: int):
        await editor.edit(progress_text(action, current, total, started))

    return callback


#: Longest single Telegram-imposed wait a send will sit through
#: before giving up (10 minutes). Longer waits fail the job with a
#: clear error instead of holding the job slot for hours.
_MAX_FLOOD_WAIT = 600
#: How many FloodWaits one send may ride out before giving up.
_MAX_FLOOD_RETRIES = 5


async def _with_flood_retry(coro_fn, editor=None):
    """Run an upload call, pausing through Telegram FloodWaits.

    `coro_fn` is a zero-arg callable returning a fresh awaitable on
    each call — a failed upload restarts from byte 0, so the call
    must be rebuildable. Retries while Telegram's required wait is
    short; re-raises when the wait exceeds _MAX_FLOOD_WAIT or after
    _MAX_FLOOD_RETRIES waits.
    """
    attempts = 0
    while True:
        try:
            return await coro_fn()
        except (FloodWait, FloodPremiumWait, SlowmodeWait) as exc:
            wait = int(getattr(exc, "value", 0) or 0)
            attempts += 1
            if wait > _MAX_FLOOD_WAIT or attempts > _MAX_FLOOD_RETRIES:
                raise
            if editor is not None:
                try:
                    await editor.edit(
                        f"Telegram asked me to wait {wait}s — resuming after…",
                        force=True,
                    )
                except Exception:
                    pass
            await asyncio.sleep(wait)


def _fmt_duration(seconds: int) -> str:
    """107 -> '1:47', 6422 -> '1:47:02'."""
    seconds = int(seconds or 0)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


async def _rich_caption(caption: str, paths: list[str]) -> str:
    """Append a size/duration/quality line to an upload caption.

    Total size covers every path (for split parts, the whole file).
    Duration + resolution + quality bucket + audio/subtitle
    languages come from ffprobing the first path when it is a
    video (best-effort — a split part's header usually still
    carries the full duration). When nothing can be measured, the
    caption is returned unchanged.

    When a single file — or one file's whole split run — is sent
    with no caption, or with its raw filename as the caption (the
    direct/custom flows), the caption head becomes the file's
    clean title ("Show S01E01") instead of the messy release
    name (v1.44.0; split runs v1.47.0).
    """
    # The name to clean: one file's basename, or a split run's stem
    # (every path a .001/.002 part of the SAME file — a mixed batch
    # never qualifies, so a folder of different files keeps its
    # folder-name caption).
    head_name = None
    if len(paths) == 1:
        head_name = os.path.basename(paths[0])
    elif len(paths) > 1 and all(is_split_part(p) for p in paths):
        stems = {p.rsplit(".", 1)[0] for p in paths}
        if len(stems) == 1:
            head_name = os.path.basename(stems.pop())
    if head_name and (not caption or caption == head_name):
        clean = clean_display(head_name)
        if clean:
            caption = clean
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    bits = []
    if total > 0:
        bits.append(human_bytes(total))
    first = paths[0] if paths else None
    if first and (guess_kind(first) == "video" or is_split_part(first)):
        meta = await run_blocking(_ffprobe, first)
        if meta.get("duration"):
            bits.append(_fmt_duration(meta["duration"]))
        if meta.get("width") and meta.get("height"):
            bits.append(f"{meta['width']}x{meta['height']}")
            bits.append(_quality_bucket(meta["height"]))
        audio = meta.get("audio_langs") or []
        if audio:
            shown = audio[:4]
            bits.append("Audio: " + ", ".join(shown)
                        + (f" +{len(audio) - 4} more"
                           if len(audio) > 4 else ""))
        subs = meta.get("sub_langs") or []
        if subs:
            shown = subs[:4]
            bits.append("Subs: " + ", ".join(shown)
                        + (f" +{len(subs) - 4} more"
                           if len(subs) > 4 else ""))
    if not bits:
        return caption
    line = " • ".join(bits)
    return f"{caption}\n{line}" if caption else line


def _split_run(paths: list[str], start: int) -> list[str] | None:
    """Consecutive .001/.002 parts of one file starting at `start`.

    Returns the run (2+ parts, numeric order) or None when the path
    at `start` is not part of a multi-part run.
    """
    first = paths[start]
    if not is_split_part(first):
        return None
    stem = first.rsplit(".", 1)[0]
    run = [first]
    j = start + 1
    while j < len(paths):
        nxt = paths[j]
        if not is_split_part(nxt) or nxt.rsplit(".", 1)[0] != stem:
            break
        run.append(nxt)
        j += 1
    if len(run) < 2:
        return None
    run.sort(key=lambda p: int(p.rsplit(".", 1)[1]))
    return run


async def send_file(client, chat_id: int, path: str, caption: str = "",
                    status_msg=None, progress_cb=None,
                    force_document: bool = False, job=None):
    """Send one local file as video / photo / audio / document.

    Returns the sent Message (or None if the client returned nothing).
    `progress_cb`, when given, is awaited with (current, total) bytes
    as Telegram reports them — in addition to the status-message
    editor, not instead of it.
    `force_document` sends a video as a plain file instead (the
    /destination "Send URL uploads as" setting); photos, audio and
    split parts are unaffected.
    `job`, when given (a core.jobs.Job), replaces the status message
    as the progress target AND makes the send cancellable: a Stop
    tap raises StopTransmission from the upload progress callback
    (Pyrogram then returns None from send_*), and this raises
    JobCancelled so the flow unwinds."""
    if job is not None:
        job.check_cancelled()
    kind = guess_kind(path)
    meta: dict = {}
    if kind in ("document", "video") and not force_document:
        # Trust the CONTENT, not the extension (v1.44.0): a video
        # wearing a wrong/no extension (or an unknown MIME in this
        # container) would leave as a document, an audio file with
        # cover art would leave as a video (its mjpeg "video"
        # stream is just the cover), and an audio file with a
        # video extension would leave as a broken video. ffprobe
        # now reports has_video (real video streams only — image
        # codecs don't count) and has_audio, so the kind follows
        # the streams. Split parts (.001/.002) are the exception:
        # they are byte ranges, not videos — even though the FIRST
        # part still carries the video header ffprobe would happily
        # recognise. Sending it as video would show a broken clip
        # with a wrong duration, so parts always stay documents.
        # When the probe itself fails ({}), the extension kind
        # stands — a failed probe must never demote a real video.
        if not is_split_part(path):
            meta = await run_blocking(_ffprobe, path)
            if meta.get("probed"):
                if meta.get("has_video"):
                    kind = "video"
                elif meta.get("has_audio"):
                    kind = "audio"
                elif kind == "video":
                    kind = "document"
    if force_document and kind == "video":
        kind = "document"
    # A job IS the editor (Job.edit has ThrottledEditor.edit's shape),
    # so upload progress lands in the shared message (v1.45.0).
    editor = (job if job is not None else
              (ThrottledEditor(status_msg) if status_msg is not None
               else None))
    callbacks = []
    if editor is not None:
        callbacks.append(_progress_callback(editor, "Uploading…"))
    if progress_cb is not None:
        callbacks.append(progress_cb)
    if job is not None:
        async def _cancel_check(current: int, total: int):
            # The documented mid-upload stop: Pyrogram catches
            # StopTransmission from a progress callback and returns
            # None from send_*, instead of the sent Message.
            if job.cancelled:
                raise StopTransmission
        callbacks.append(_cancel_check)

    async def progress(current: int, total: int):
        for cb in callbacks:
            await cb(current, total)

    progress = progress if callbacks else None

    sent = None
    if kind == "video":
        if not meta:
            meta = await run_blocking(_ffprobe, path)
        thumb = await _poster_thumb(path)
        if thumb is None:
            thumb = await run_blocking(_make_thumb, path)
        sent = await _with_flood_retry(
            lambda: client.send_video(
                chat_id, path, caption=caption, supports_streaming=True,
                duration=meta.get("duration", 0),
                width=meta.get("width", 0), height=meta.get("height", 0),
                thumb=thumb, progress=progress),
            editor)
        if thumb:
            try:
                os.remove(thumb)
            except OSError:
                pass
    elif kind == "photo":
        sent = await _with_flood_retry(
            lambda: client.send_photo(chat_id, path, caption=caption,
                                      progress=progress),
            editor)
    elif kind == "audio":
        sent = await _with_flood_retry(
            lambda: client.send_audio(chat_id, path, caption=caption,
                                      progress=progress),
            editor)
    else:
        sent = await _with_flood_retry(
            lambda: client.send_document(chat_id, path, caption=caption,
                                         progress=progress),
            editor)
    # None + a cancelled job means the Stop tap cut this upload
    # short (StopTransmission) — unwind the flow as cancelled, not
    # as a silent no-send.
    if sent is None and job is not None and job.cancelled:
        raise JobCancelled()
    return sent


async def _attach_mediainfo_button(sent) -> None:
    """Add a 📊 MediaInfo button under a single uploaded file.

    The button's callback (`mi:<chat>:<msg>`, handled in
    plugins/mediainfo.py) probes this very message later.
    """
    try:
        chat = getattr(sent, "chat", None)
        if chat is None or not getattr(sent, "id", None):
            return
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📊 MediaInfo",
                callback_data=f"mi:{chat.id}:{sent.id}")]])
        await sent.edit_reply_markup(markup)
    except Exception:
        pass


async def send_files(client, chat_id: int, paths: list[str], caption: str = "",
                     status_msg=None, mediainfo_button: bool = False,
                     progress_cb=None,
                     force_document: bool = False, job=None) -> list:
    """Send a job's resulting files; returns the sent Messages.

    `force_document` sends videos as plain files instead (the
    /destination "Send URL uploads as" setting); photos, audio and
    split parts are unaffected.

    Photo sets (carousels, gallery dumps) go out as media groups of up to
    10; everything else is sent one by one. Split .001/.002 parts
    used to go as a media group too, but a group takes a caption
    only on its first item and Telegram reports no live progress
    for it — so since v1.47.0 parts go one by one, each with the
    caption plus a "Part N of M" line and its own live progress
    bar. The caption rides on the first item only for other
    multi-file sends, like Telegram clients do it, and gains a
    size/duration/quality line (see _rich_caption). Every send rides
    out short Telegram FloodWaits instead of failing the job.

    With mediainfo_button=True and exactly one resulting message, a
    📊 MediaInfo callback button is attached under it.

    `progress_cb`, when given, is awaited with (sent_so_far, total)
    across ALL files — cumulative, so a caller can show one bar and one
    speed for the whole upload. Photo media groups report no live
    progress (Telegram's API has no progress hook for them), so the
    callback fires once per finished group instead of streaming.

    `job`, when given (a core.jobs.Job), replaces the status message
    as the progress target and makes the run cancellable between
    files/groups (a media group already sending can't be cut).
    """
    if job is not None:
        job.check_cancelled()
    sent_all: list = []
    if not paths:
        return sent_all
    caption = await _rich_caption(caption, paths)
    group_editor = (job if job is not None else
                    (ThrottledEditor(status_msg)
                     if status_msg is not None else None))

    total_all = 0
    sizes: dict[str, int] = {}
    if progress_cb is not None:
        for path in paths:
            try:
                sizes[path] = os.path.getsize(path)
            except OSError:
                sizes[path] = 0
        total_all = sum(sizes.values())
    sent_bytes = 0

    def _per_file(base: int):
        async def cb(current: int, total: int):
            await progress_cb(base + current, total_all)
        return cb

    if len(paths) == 1:
        sent = await send_file(client, chat_id, paths[0], caption, status_msg,
                               progress_cb=_per_file(0)
                               if progress_cb is not None else None,
                               force_document=force_document, job=job)
        if sent is not None:
            sent_all.append(sent)
        if mediainfo_button and len(sent_all) == 1:
            await _attach_mediainfo_button(sent_all[0])
        stats.record_paths("uploaded", paths)
        return sent_all

    photos = [p for p in paths if guess_kind(p) == "photo"]
    others = [p for p in paths if guess_kind(p) != "photo"]
    first = True

    for i in range(0, len(photos), 10):
        if job is not None:
            job.check_cancelled()
        chunk = photos[i:i + 10]
        if len(chunk) == 1:
            sent = await send_file(client, chat_id, chunk[0],
                                   caption if first else "", status_msg,
                                   progress_cb=_per_file(sent_bytes)
                                   if progress_cb is not None else None,
                                   force_document=force_document, job=job)
            if sent is not None:
                sent_all.append(sent)
        else:
            media = [
                InputMediaPhoto(p, caption=caption if (first and j == 0) else "")
                for j, p in enumerate(chunk)
            ]
            group = await _with_flood_retry(
                lambda media=media: client.send_media_group(chat_id, media),
                group_editor)
            if group:
                sent_all.extend(group)
            # No live progress for a media group — report it as done
            # in one jump once the group is sent.
            if progress_cb is not None:
                sent_bytes += sum(sizes.get(p, 0) for p in chunk)
                await progress_cb(sent_bytes, total_all)
        if progress_cb is not None and len(chunk) == 1:
            sent_bytes += sizes.get(chunk[0], 0)
        first = False
        await asyncio.sleep(1)

    i = 0
    while i < len(others):
        if job is not None:
            job.check_cancelled()
        path = others[i]
        run = _split_run(others, i)
        if run is not None:
            # Consecutive .001/.002 parts of one file go out ONE BY
            # ONE (v1.47.0), not as a media group: a group takes a
            # caption only on its first item and Telegram reports no
            # live upload progress for it, so a 13 GB upload sat at
            # a bare "Uploading…" for hours. One by one, every part
            # carries the caption (plus its "Part N of M" line) and
            # its own live progress bar; the cost is separate
            # messages instead of one album.
            total_parts = len(run)
            for n, part in enumerate(run, 1):
                if job is not None:
                    job.check_cancelled()
                base = caption if first else ""
                part_caption = (f"{base}\nPart {n} of {total_parts}"
                                if base else f"Part {n} of {total_parts}")
                sent = await send_file(
                    client, chat_id, part, part_caption, status_msg,
                    progress_cb=_per_file(sent_bytes)
                    if progress_cb is not None else None,
                    force_document=force_document, job=job)
                if sent is not None:
                    sent_all.append(sent)
                if progress_cb is not None:
                    sent_bytes += sizes.get(part, 0)
                await asyncio.sleep(1)
            first = False
            i += len(run)
            continue
        sent = await send_file(client, chat_id, path,
                               caption if first else "", status_msg,
                               progress_cb=_per_file(sent_bytes)
                               if progress_cb is not None else None,
                               force_document=force_document, job=job)
        if sent is not None:
            sent_all.append(sent)
        if progress_cb is not None:
            sent_bytes += sizes.get(path, 0)
        first = False
        await asyncio.sleep(1)
        i += 1

    if mediainfo_button and len(sent_all) == 1:
        await _attach_mediainfo_button(sent_all[0])
    stats.record_paths("uploaded", paths)
    return sent_all


async def send_files_split(bot_client, chat_id: int, paths: list[str],
                           feature: str, caption: str = "",
                           status_msg=None, job=None,
                           user_id=None) -> list[str]:
    """Split oversize files, then send_files_routed().

    Every file over Telegram's cap is chopped into .001/.002
    parts first (v1.40.0: a 10 GB video out of a zip arrives as
    mkv.001/.002 instead of failing the cap). Files within the
    cap pass through untouched. Returns the paths that were
    sent, so the caller can delete them (split parts included —
    the original is already gone, deleted by the splitter).
    `job`, when given, is the progress target + cancel handle
    (see send_files). `user_id`, when given, picks that user's
    own destination/send-as settings (v1.45.0)."""
    if job is not None:
        job.check_cancelled()
    send_paths: list[str] = []
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        async with splitter.big_file_guard(splitter.needs_split(size)):
            send_paths.extend(
                await splitter.maybe_split([path], job, status_msg))
    await send_files_routed(bot_client, chat_id, send_paths, feature,
                            caption=caption, status_msg=status_msg,
                            job=job, user_id=user_id)
    return send_paths


async def send_files_routed(bot_client, chat_id: int, paths: list[str],
                            feature: str, caption: str = "",
                            status_msg=None,
                            mediainfo_button: bool = False,
                            force_document: bool | None = None,
                            job=None, user_id=None) -> list:
    """send_files() to the destination chosen for `feature`.

    Destinations (core/settings.py, /destination command):
      "bot"   — the bot sends the files to the requester's chat.
      "saved" — the *user account* uploads them to its own Saved
                Messages (Destiny-style). Needs SESSION_STRING; when no
                user session is configured, falls back to "bot".
    Defaults: URL uploads go to "bot", restricted downloads to "saved"
    (owner only — sudo users always get "bot").
    The progress editor keeps working either way: the status message
    belongs to the bot, only the uploading client changes.

    `force_document` decides whether videos go out as plain files:
    None (default) follows the feature's "Send as" setting — only
    "uploader" has one, everything else sends videos as video.
    An explicit True/False overrides the setting (live recordings
    and samples always pass False: they are meant to be watched).
    `job`, when given, is the progress target + cancel handle
    (see send_files). `user_id`, when given, picks that user's own
    destination/send-as settings (v1.45.0); without it the shared
    global settings apply.
    """
    from core import settings
    from core.userclient import SessionNotConfigured, get_user_client

    if job is not None:
        job.check_cancelled()
    if force_document is None:
        force_document = settings.get_send_as(feature, user_id) \
            == "document"
    if settings.get_dest(feature, user_id) == "saved":
        try:
            user = await get_user_client()
        except SessionNotConfigured:
            user = None
        if user is not None:
            return await send_files(user, "me", paths, caption=caption,
                                    status_msg=status_msg,
                                    mediainfo_button=False,
                                    force_document=force_document,
                                    job=job)
    return await send_files(bot_client, chat_id, paths, caption=caption,
                            status_msg=status_msg,
                            mediainfo_button=mediainfo_button,
                            force_document=force_document,
                            job=job)
