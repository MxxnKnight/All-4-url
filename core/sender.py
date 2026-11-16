"""Uploading finished files back to Telegram, with progress + thumbnails."""

import asyncio
import json
import os
import time

from pyrogram.errors import FloodPremiumWait, FloodWait, SlowmodeWait
from pyrogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                            InputMediaDocument, InputMediaPhoto)

from core import stats
from core.splitter import is_split_part
from core.utils import (ThrottledEditor, guess_kind, human_bytes,
                        progress_text, run_blocking)


def _ffprobe(path: str) -> dict:
    """Best-effort width/height/duration for a video ({} when unavailable)."""
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(out.stdout or "{}")
        info = {}
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width") or 0
                info["height"] = stream.get("height") or 0
                break
        duration = float(data.get("format", {}).get("duration") or 0)
        if duration:
            info["duration"] = int(duration)
        return info
    except Exception:
        return {}


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
    Duration + resolution come from ffprobing the first path when
    it is a video (best-effort — a split part's header usually still
    carries the full duration). When nothing can be measured, the
    caption is returned unchanged.
    """
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
                    status_msg=None, progress_cb=None):
    """Send one local file as video / photo / audio / document.

    Returns the sent Message (or None if the client returned nothing).
    `progress_cb`, when given, is awaited with (current, total) bytes
    as Telegram reports them — in addition to the status-message
    editor, not instead of it."""
    kind = guess_kind(path)
    meta: dict = {}
    if kind == "document":
        # A video wearing a wrong/no extension (or an unknown MIME in
        # this container) would leave as a document. Trust the CONTENT
        # instead: if ffprobe finds a video stream, send it as video.
        # Split parts (.001/.002) are the exception: they are byte
        # ranges, not videos — even though the FIRST part still
        # carries the video header ffprobe would happily recognise.
        # Sending it as video would show a broken clip with a wrong
        # duration, so parts always stay documents.
        if not is_split_part(path):
            meta = await run_blocking(_ffprobe, path)
            if meta.get("duration") or meta.get("width"):
                kind = "video"
    editor = ThrottledEditor(status_msg) if status_msg is not None else None
    callbacks = []
    if editor is not None:
        callbacks.append(_progress_callback(editor, "Uploading…"))
    if progress_cb is not None:
        callbacks.append(progress_cb)

    async def progress(current: int, total: int):
        for cb in callbacks:
            await cb(current, total)

    progress = progress if callbacks else None

    sent = None
    if kind == "video":
        if not meta:
            meta = await run_blocking(_ffprobe, path)
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
                     progress_cb=None) -> list:
    """Send a job's resulting files; returns the sent Messages.

    Photo sets (carousels, gallery dumps) go out as media groups of up to
    10, and so do consecutive .001/.002 parts of one split file;
    everything else is sent one by one. The caption rides on the
    first item only, like Telegram clients do it, and gains a
    size/duration/quality line (see _rich_caption). Every send rides
    out short Telegram FloodWaits instead of failing the job.

    With mediainfo_button=True and exactly one resulting message, a
    📊 MediaInfo callback button is attached under it.

    `progress_cb`, when given, is awaited with (sent_so_far, total)
    across ALL files — cumulative, so a caller can show one bar and one
    speed for the whole upload. Media groups report no live progress
    (Telegram's API has no progress hook for them), so the callback
    fires once per finished group instead of streaming.
    """
    sent_all: list = []
    if not paths:
        return sent_all
    caption = await _rich_caption(caption, paths)
    group_editor = (ThrottledEditor(status_msg)
                    if status_msg is not None else None)

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
                               if progress_cb is not None else None)
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
        chunk = photos[i:i + 10]
        if len(chunk) == 1:
            sent = await send_file(client, chat_id, chunk[0],
                                   caption if first else "", status_msg,
                                   progress_cb=_per_file(sent_bytes)
                                   if progress_cb is not None else None)
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
        path = others[i]
        run = _split_run(others, i)
        if run is not None:
            # Consecutive .001/.002 parts of one file go out as a
            # single media group (chunked at 10) instead of one
            # message per part.
            for k in range(0, len(run), 10):
                chunk = run[k:k + 10]
                media = [
                    InputMediaDocument(
                        p, caption=caption if (first and m == 0) else "")
                    for m, p in enumerate(chunk)
                ]
                group = await _with_flood_retry(
                    lambda media=media: client.send_media_group(
                        chat_id, media),
                    group_editor)
                if group:
                    sent_all.extend(group)
                # No live progress for a media group — report it as
                # done in one jump once the group is sent.
                if progress_cb is not None:
                    sent_bytes += sum(sizes.get(p, 0) for p in chunk)
                    await progress_cb(sent_bytes, total_all)
                first = False
                await asyncio.sleep(1)
            i += len(run)
            continue
        sent = await send_file(client, chat_id, path,
                               caption if first else "", status_msg,
                               progress_cb=_per_file(sent_bytes)
                               if progress_cb is not None else None)
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


async def send_files_routed(bot_client, chat_id: int, paths: list[str],
                            feature: str, caption: str = "",
                            status_msg=None,
                            mediainfo_button: bool = False) -> list:
    """send_files() to the destination chosen for `feature`.

    Destinations (core/settings.py, /destination command):
      "bot"   — the bot sends the files to the requester's chat (default).
      "saved" — the *user account* uploads them to its own Saved
                Messages (Destiny-style). Needs SESSION_STRING; when no
                user session is configured, falls back to "bot".
    The progress editor keeps working either way: the status message
    belongs to the bot, only the uploading client changes.
    """
    from core import settings
    from core.userclient import SessionNotConfigured, get_user_client

    if settings.get_dest(feature) == "saved":
        try:
            user = await get_user_client()
        except SessionNotConfigured:
            user = None
        if user is not None:
            return await send_files(user, "me", paths, caption=caption,
                                    status_msg=status_msg,
                                    mediainfo_button=False)
    return await send_files(bot_client, chat_id, paths, caption=caption,
                            status_msg=status_msg,
                            mediainfo_button=mediainfo_button)
