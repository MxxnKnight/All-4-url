"""Shared helpers: formatting, progress reporting, temp dirs, owner filter."""

import asyncio
import mimetypes
import os
import shutil
import tempfile
import time

from pyrogram import filters

import config

# One global gate so the small free-tier host is never swamped: at most
# config.MAX_JOBS downloads/uploads run at the same time, the rest queue.
JOB_SEMAPHORE = asyncio.Semaphore(config.MAX_JOBS)


async def _is_owner(_, __, message) -> bool:
    user = getattr(message, "from_user", None)
    return bool(user and config.OWNER_ID and user.id == config.OWNER_ID)


#: Drop-in filter for handlers: `filters.private & owner_only`
owner_only = filters.create(_is_owner, "OwnerOnly")


def is_owner_id(user_id) -> bool:
    return bool(user_id and config.OWNER_ID and user_id == config.OWNER_ID)


async def react(message, emoji: str) -> None:
    """React to a message with an emoji — pure decoration, never fatal.

    WzGram 3.1.5 has Message.react(emoji) (verified: it delegates to
    Client.send_reaction). If that shortcut is unavailable or fails, we
    fall back to the raw client call, and any remaining failure is
    swallowed: a missing reaction must never break an actual transfer.
    """
    try:
        await message.react(emoji)
        return
    except Exception:
        pass
    try:
        client = getattr(message, "_client", None)
        chat = getattr(message, "chat", None)
        if client is not None and chat is not None:
            await client.send_reaction(chat_id=chat.id,
                                       message_id=message.id, emoji=emoji)
    except Exception:
        pass


async def react_to(client, chat_id: int, message_id: int, emoji: str) -> None:
    """react() when all we have is a client + ids (button callbacks)."""
    try:
        await client.send_reaction(chat_id=chat_id, message_id=message_id,
                                   emoji=emoji)
    except Exception:
        pass


def human_bytes(size) -> str:
    if not size:
        return "0 B"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} PB"


def human_time(seconds) -> str:
    if seconds is None or seconds == float("inf") or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def progress_bar(percent: float, width: int = 12) -> str:
    filled = int(width * max(0.0, min(100.0, percent)) / 100)
    return "█" * filled + "░" * (width - filled)


def progress_text(action: str, current: int, total: int, started: float) -> str:
    """One status message body for a download/upload."""
    elapsed = max(time.time() - started, 0.01)
    speed = current / elapsed
    if total and total > 0:
        percent = current * 100 / total
        eta = (total - current) / speed if speed > 0 else None
        return (
            f"{action}\n"
            f"{progress_bar(percent)} {percent:.1f}%\n"
            f"{human_bytes(current)} / {human_bytes(total)}\n"
            f"Speed: {human_bytes(speed)}/s · ETA: {human_time(eta)}"
        )
    return (
        f"{action}\n"
        f"{human_bytes(current)} downloaded\n"
        f"Speed: {human_bytes(speed)}/s"
    )


class ThrottledEditor:
    """Edits a status message at most once every `interval` seconds.

    Telegram punishes rapid edits (FloodWait) and the task spec asks for
    >= 3 s between progress edits, so we use 3.5 s. The final edit can be
    forced with `force=True`.
    """

    def __init__(self, message, interval: float = 3.5):
        self.message = message
        self.interval = interval
        self._last = 0.0
        self._last_text = ""

    async def edit(self, text: str, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last < self.interval or text == self._last_text):
            return
        self._last = now
        self._last_text = text
        try:
            await self.message.edit_text(text)
        except Exception:
            # Message deleted / not modified / flood: progress is cosmetic,
            # never let it crash the actual transfer.
            pass


def make_job_dir() -> str:
    """A fresh per-job directory inside DOWNLOAD_DIR."""
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    return tempfile.mkdtemp(prefix="job_", dir=config.DOWNLOAD_DIR)


def cleanup_dir(path: str) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def list_files(root: str) -> list[str]:
    """Every file under a job dir (extractors may nest subfolders)."""
    found = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith((".part", ".ytdl", ".tmp")):
                continue
            found.append(os.path.join(dirpath, name))
    return sorted(found)


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aac"}


def guess_kind(path: str) -> str:
    """'video' | 'photo' | 'audio' | 'document' from mime / extension."""
    mime, _ = mimetypes.guess_type(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS or (mime and mime.startswith("video/")):
        return "video"
    if ext in PHOTO_EXTS or (mime and mime.startswith("image/") and ext != ".gif"):
        return "photo"
    if ext in AUDIO_EXTS or (mime and mime.startswith("audio/")):
        return "audio"
    return "document"


async def run_blocking(func, *args):
    """Run a blocking callable in the shared thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)
