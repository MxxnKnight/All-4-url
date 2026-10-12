"""URL uploader — the resolver chain.

For any non-Telegram URL the owner sends, engines try in this order:

  Terabox domain      -> custom Terabox extractor (core/extractors/terabox)
  Instagram /p/ post  -> yt-dlp metadata pass (carousel images)
                         -> gallery-dl -> yt-dlp -> scrape
  Instagram /reel/    -> yt-dlp -> gallery-dl -> scrape
  Direct file URL     -> streamed straight down (core/extractors/direct)
  Everything else     -> yt-dlp (with format buttons when it finds
                         several resolutions) -> gallery-dl -> scrape

Only one engine's success is needed; failures cascade to the next, and
the final error message tells the owner what each engine said.
"""

import asyncio
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from core.extractors import gallery_engine, scrape, terabox
from core.extractors.direct import check_direct, download_file
from core.extractors.ytdlp_engine import (YtdlpError, best_formats,
                                          carousel_image_urls, download,
                                          extract_info)
from core.sender import send_files
from core.utils import (JOB_SEMAPHORE, ThrottledEditor, cleanup_dir,
                        human_bytes, is_owner_id, list_files, make_job_dir,
                        owner_only, progress_text, react, react_to)

URL_RE = re.compile(r"https?://[^\s]+")

# Jobs waiting on a format-button tap: job_id -> job dict
PENDING: dict[str, dict] = {}
_counter = 0


def _next_job_id() -> str:
    global _counter
    _counter += 1
    return f"{int(time.time()) % 100000}{_counter}"


def _extract_url(message: Message) -> str | None:
    text = message.text or ""
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(").,>]\"'")
    if "t.me/" in url or "telegram.me/" in url:
        return None  # Telegram links belong to the restricted plugin
    return url


# ---------------------------------------------------------------- Terabox UI

async def _terabox_flow(client: Client, message: Message, url: str,
                        status: Message, editor: ThrottledEditor) -> bool:
    """Returns True when a file picker was shown (job not finished yet)."""
    files = await terabox.list_files(url)
    if len(files) == 1:
        await _terabox_download(client, message, url, 0, files[0], status, editor)
        return False
    job_id = _next_job_id()
    PENDING[job_id] = {"type": "terabox", "url": url, "chat_id": message.chat.id,
                       "origin_chat": message.chat.id, "origin_msg": message.id}
    buttons = [
        [InlineKeyboardButton(
            f"{f['name'][:40]} · {human_bytes(f['size'])}",
            callback_data=f"tera:{job_id}:{i}")]
        for i, f in enumerate(files[:10])
    ]
    await status.edit_text(
        "This Terabox share has several files — pick one:",
        reply_markup=InlineKeyboardMarkup(buttons))
    return True


async def _terabox_download(client, message, url, index, info, status, editor):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        async def progress(done, total):
            await editor.edit(progress_text("Downloading from Terabox…",
                                            done, total, started))
        try:
            path = await terabox.download(url, job_dir, index=index,
                                          progress_cb=progress)
            await editor.edit("Uploading…", force=True)
            await send_files(client, message.chat.id, [path],
                             caption=f"Terabox · {info['name'] if info else ''}",
                             status_msg=status, mediainfo_button=True)
            await status.delete()
        finally:
            cleanup_dir(job_dir)


# ---------------------------------------------------------------- yt-dlp flow

async def _ytdlp_flow(client: Client, message: Message, url: str,
                      status: Message, editor: ThrottledEditor,
                      ask_format: bool = True) -> bool:
    """extract_info first; ask which format when there is a real choice.

    Returns True when a format picker was shown (job not finished yet)."""
    info = await extract_info(url)
    title = info.get("title") or "Download"
    formats = best_formats(info) if info.get("formats") else []
    heights = {f["height"] for f in formats}

    if ask_format and len(heights) >= 2:
        job_id = _next_job_id()
        PENDING[job_id] = {"type": "ytdlp", "url": url,
                           "chat_id": message.chat.id, "title": title,
                           "origin_chat": message.chat.id,
                           "origin_msg": message.id}
        buttons = [[InlineKeyboardButton(
            "⭐ Best quality", callback_data=f"fmt:{job_id}:best")]]
        for fmt in formats[:5]:
            size = f" · {human_bytes(fmt['filesize'])}" if fmt["filesize"] else ""
            buttons.append([InlineKeyboardButton(
                f"{fmt['height']}p{size}",
                callback_data=f"fmt:{job_id}:{fmt['format_id']}")])
        buttons.append([InlineKeyboardButton(
            "🎧 Audio only (mp3)", callback_data=f"fmt:{job_id}:audio")])
        await status.edit_text(
            f"**{title[:120]}**\nPick a quality:",
            reply_markup=InlineKeyboardMarkup(buttons))
        return True

    await _ytdlp_download(client, message, url, title, status, editor)
    return False


async def _ytdlp_download(client, message, url, title, status, editor,
                          format_id=None, audio_only=False):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        async def progress(done, total):
            await editor.edit(progress_text("Downloading…", done, total, started))

        try:
            paths = await download(url, job_dir, format_id=format_id,
                                   audio_only=audio_only, progress_cb=progress)
            await editor.edit("Uploading…", force=True)
            await send_files(client, message.chat.id, paths,
                             caption=title[:200], status_msg=status, mediainfo_button=True)
            await status.delete()
        finally:
            cleanup_dir(job_dir)


# ---------------------------------------------------------------- fallbacks

async def _gallery_flow(client, message, url, status):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        try:
            await status.edit_text("Fetching with gallery-dl…")
            paths = await gallery_engine.download(url, job_dir)
            await send_files(client, message.chat.id, paths,
                             caption=url, status_msg=status, mediainfo_button=True)
            await status.delete()
        finally:
            cleanup_dir(job_dir)


async def _direct_flow(client, message, url, info, status, editor):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        async def progress(done, total):
            await editor.edit(progress_text("Downloading…", done, total, started))

        try:
            path = await download_file(info.get("final_url") or url, job_dir,
                                       progress_cb=progress,
                                       filename=info.get("filename"))
            await editor.edit("Uploading…", force=True)
            await send_files(client, message.chat.id, [path],
                             caption=info.get("filename", ""), status_msg=status, mediainfo_button=True)
            await status.delete()
        finally:
            cleanup_dir(job_dir)


async def _scrape_flow(client, message, url, status, editor):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        async def progress(done, total):
            await editor.edit(progress_text("Downloading…", done, total, started))

        try:
            path = await scrape.download(url, job_dir, progress_cb=progress)
            await editor.edit("Uploading…", force=True)
            await send_files(client, message.chat.id, [path],
                             caption=url, status_msg=status, mediainfo_button=True)
            await status.delete()
        finally:
            cleanup_dir(job_dir)


async def _instagram_carousel(client, message, url, status) -> bool:
    """Image posts: yt-dlp metadata carries every carousel image URL."""
    try:
        info = await extract_info(url, ignore_no_formats=True)
    except YtdlpError:
        return False
    urls = carousel_image_urls(info)
    if not urls:
        return False
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        try:
            await status.edit_text(f"Found {len(urls)} images — downloading…")
            paths = []
            for i, img_url in enumerate(urls):
                paths.append(await download_file(
                    img_url, job_dir, filename=f"instagram_{i + 1}.jpg"))
            await send_files(client, message.chat.id, paths,
                             caption=url, status_msg=status, mediainfo_button=True)
            await status.delete()
            return True
        finally:
            cleanup_dir(job_dir)


# ---------------------------------------------------------------- entry point

@Client.on_message(filters.private & owner_only & filters.text
                   & ~filters.command(["start", "help", "batch", "cancel",
                                       "mediainfo"]))
async def url_handler(client: Client, message: Message):
    url = _extract_url(message)
    if not url:
        return
    await react(message, "👀")
    status = await message.reply_text("Working on it…")
    editor = ThrottledEditor(status)
    outcome = await _dispatch(client, message, url, status, editor)
    if outcome == "done":
        await react(message, "👍")
    elif outcome == "failed":
        await react(message, "👎")
    # "pending" = a picker is on screen; the reaction comes from the
    # button callback when the job actually finishes.


async def _dispatch(client: Client, message: Message, url: str,
                    status: Message, editor: ThrottledEditor) -> str:
    """Run the resolver chain. Returns "done" | "pending" | "failed"."""
    errors: list[str] = []
    try:
        # 1) Terabox gets its own extractor, always first for its domains.
        if terabox.is_terabox_url(url):
            try:
                picker = await _terabox_flow(client, message, url,
                                             status, editor)
                return "pending" if picker else "done"
            except Exception as exc:
                await status.edit_text(f"Terabox failed: {exc}")
                return "failed"

        is_post = "instagram.com/p/" in url
        is_reel = "instagram.com/reel" in url

        # 2) Instagram photo posts are images, not videos.
        if is_post:
            if await _instagram_carousel(client, message, url, status):
                return "done"
            try:
                await _gallery_flow(client, message, url, status)
                return "done"
            except Exception as exc:
                errors.append(f"gallery-dl: {exc}")

        # 3) A raw file link needs no extractor at all.
        direct_info = await check_direct(url)
        if direct_info:
            try:
                await _direct_flow(client, message, url, direct_info,
                                   status, editor)
                return "done"
            except Exception as exc:
                errors.append(f"direct: {exc}")

        # 4) yt-dlp — reels and every other video/audio site.
        try:
            picker = await _ytdlp_flow(client, message, url, status, editor,
                                        ask_format=not is_reel)
            return "pending" if picker else "done"
        except Exception as exc:
            errors.append(f"yt-dlp: {exc}")

        # 5) gallery-dl also covers many video-page galleries as backup.
        if not is_post:
            try:
                await _gallery_flow(client, message, url, status)
                return "done"
            except Exception as exc:
                errors.append(f"gallery-dl: {exc}")

        # 6) Last resort: scrape the page's meta tags.
        try:
            await _scrape_flow(client, message, url, status, editor)
            return "done"
        except Exception as exc:
            errors.append(f"scrape: {exc}")

        await status.edit_text(
            "I couldn't download that with any engine:\n"
            + "\n".join(f"• {e}" for e in errors)
            + "\n\nIf it's Instagram/a login-walled site, it may need a "
              "cookies.txt — see the README.")
        return "failed"
    except Exception as exc:  # never leave a stuck status message
        try:
            await status.edit_text(f"Something went wrong: {exc}")
        except Exception:
            pass
        return "failed"


# ---------------------------------------------------------------- callbacks

@Client.on_callback_query(filters.regex(r"^(fmt|tera):"))
async def format_callback(client: Client, query: CallbackQuery):
    if not is_owner_id(query.from_user.id if query.from_user else None):
        await query.answer("Not for you.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer("Stale button.")
        return
    kind, job_id = parts[0], parts[1]
    job = PENDING.pop(job_id, None)
    if not job:
        await query.answer("This job expired — send the link again.",
                           show_alert=True)
        return
    await query.answer("Starting…")
    status = query.message
    editor = ThrottledEditor(status)
    fake_message = query.message  # carries chat.id for the send helpers

    try:
        if kind == "tera":
            index = int(parts[2])
            files = await terabox.list_files(job["url"])
            info = files[index] if index < len(files) else None
            await _terabox_download(client, fake_message, job["url"], index,
                                    info, status, editor)
        else:
            choice = parts[2]
            await _ytdlp_download(
                client, fake_message, job["url"], job.get("title", "Download"),
                status, editor,
                format_id=None if choice in ("best", "audio") else choice,
                audio_only=choice == "audio")
        if job.get("origin_chat") and job.get("origin_msg"):
            await react_to(client, job["origin_chat"], job["origin_msg"], "👍")
    except Exception as exc:
        if job.get("origin_chat") and job.get("origin_msg"):
            await react_to(client, job["origin_chat"], job["origin_msg"], "👎")
        try:
            await status.edit_text(f"Download failed: {exc}")
        except Exception:
            pass
