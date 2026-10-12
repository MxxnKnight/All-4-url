"""Uploading finished files back to Telegram, with progress + thumbnails."""

import asyncio
import json
import os
import time

from pyrogram.types import InputMediaPhoto

from core.utils import ThrottledEditor, guess_kind, progress_text, run_blocking


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


async def send_file(client, chat_id: int, path: str, caption: str = "",
                    status_msg=None) -> None:
    """Send one local file as video / photo / audio / document."""
    kind = guess_kind(path)
    editor = ThrottledEditor(status_msg) if status_msg is not None else None
    progress = _progress_callback(editor, "Uploading…") if editor else None

    if kind == "video":
        meta, thumb = await run_blocking(_ffprobe, path), None
        thumb = await run_blocking(_make_thumb, path)
        await client.send_video(
            chat_id, path, caption=caption, supports_streaming=True,
            duration=meta.get("duration", 0),
            width=meta.get("width", 0), height=meta.get("height", 0),
            thumb=thumb, progress=progress,
        )
        if thumb:
            try:
                os.remove(thumb)
            except OSError:
                pass
    elif kind == "photo":
        await client.send_photo(chat_id, path, caption=caption, progress=progress)
    elif kind == "audio":
        await client.send_audio(chat_id, path, caption=caption, progress=progress)
    else:
        await client.send_document(chat_id, path, caption=caption, progress=progress)


async def send_files(client, chat_id: int, paths: list[str], caption: str = "",
                     status_msg=None) -> None:
    """Send a job's resulting files.

    Photo sets (carousels, gallery dumps) go out as media groups of up to
    10, everything else is sent one by one. The caption rides on the
    first item only, like Telegram clients do it.
    """
    if not paths:
        return
    if len(paths) == 1:
        await send_file(client, chat_id, paths[0], caption, status_msg)
        return

    photos = [p for p in paths if guess_kind(p) == "photo"]
    others = [p for p in paths if guess_kind(p) != "photo"]
    first = True

    for i in range(0, len(photos), 10):
        chunk = photos[i:i + 10]
        if len(chunk) == 1:
            await send_file(client, chat_id, chunk[0],
                            caption if first else "", status_msg)
        else:
            media = [
                InputMediaPhoto(p, caption=caption if (first and j == 0) else "")
                for j, p in enumerate(chunk)
            ]
            await client.send_media_group(chat_id, media)
        first = False
        await asyncio.sleep(1)

    for path in others:
        await send_file(client, chat_id, path, caption if first else "", status_msg)
        first = False
        await asyncio.sleep(1)
