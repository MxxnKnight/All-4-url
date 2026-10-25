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
from urllib.parse import urlparse

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from core.extractors import gallery_engine, scrape, terabox
from core import stats, upage
from core.extractors.direct import (check_direct, download_file,
                                     is_playlist_url)
from core.extractors.ytdlp_engine import (YtdlpError, best_formats,
                                          carousel_image_urls, download,
                                          extract_info)
from core.sender import send_files_routed
from core.utils import (JOB_SEMAPHORE, SizeBlocked, ThrottledEditor,
                        check_size, cleanup_dir, human_bytes, is_admin_id,
                        list_files, make_job_dir, admin_only, progress_text,
                        react, react_to)

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
        check_size(files[0].get("size") or 0, files[0].get("name") or "file")
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
    via_note = (" (via FileSnag — its links expire in a few hours, "
                "so pick promptly)" if files[0].get("via") == "filesnag"
                else "")
    await status.edit_text(
        f"This Terabox share has several files — pick one:{via_note}",
        reply_markup=InlineKeyboardMarkup(buttons))
    return True


async def _terabox_download(client, message, url, index, info, status, editor):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        via = (info or {}).get("via")
        progress_label = ("Downloading from Terabox (via FileSnag)…"
                          if via == "filesnag"
                          else "Downloading from Terabox…")

        async def progress(done, total):
            await editor.edit(progress_text(progress_label,
                                            done, total, started))
        try:
            path = await terabox.download(url, job_dir, index=index,
                                          progress_cb=progress)
            await editor.edit("Uploading…", force=True)
            await send_files_routed(client, message.chat.id, [path], "uploader",
                             caption=f"Terabox · {info['name'] if info else ''}",
                             status_msg=status)
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
                           "formats": formats,
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

    if formats:
        # No picker (reels / single format): still refuse a giant file
        # before spending the download on it.
        check_size(formats[0].get("filesize") or 0, title)
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
            await send_files_routed(client, message.chat.id, paths, "uploader",
                             caption=title[:200], status_msg=status)
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
            await send_files_routed(client, message.chat.id, paths, "uploader",
                             caption=url, status_msg=status)
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
            await send_files_routed(client, message.chat.id, [path], "uploader",
                             caption=info.get("filename", ""), status_msg=status)
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
            media_url = await scrape.find_media_url(url)
            if is_playlist_url(media_url):
                # The page embeds a stream manifest, not a file — let
                # yt-dlp fetch the real segments instead of saving the
                # playlist text.
                await status.edit_text(
                    "Found a stream playlist — downloading the video…")
                paths = await download(media_url, job_dir,
                                       progress_cb=progress)
            else:
                path = await download_file(media_url, job_dir,
                                           progress_cb=progress,
                                           headers={"Referer": url})
                paths = [path]
            await editor.edit("Uploading…", force=True)
            await send_files_routed(client, message.chat.id, paths, "uploader",
                             caption=url, status_msg=status)
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
            await send_files_routed(client, message.chat.id, paths, "uploader",
                             caption=url, status_msg=status)
            await status.delete()
            return True
        finally:
            cleanup_dir(job_dir)


# ---------------------------------------------------------------- entry point

@Client.on_message(filters.private & admin_only & filters.text
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
        stats.record_job("uploader", ok=True)
    elif outcome == "failed":
        await react(message, "👎")
        stats.record_job("uploader", ok=False)
    # "pending" = a picker is on screen; the reaction comes from the
    # button callback when the job actually finishes.


async def _upage_local_flow(client: Client, message: Message, url: str,
                            status: Message) -> str:
    """Upload a web-upload file to Telegram from the disk copy.

    The link is single-use by design: the file is deleted the moment
    this upload succeeds, so the same link can never upload twice.
    A failed Telegram upload leaves the file (and the link) intact
    for a retry.
    """
    token = urlparse(url).path[len("/u/"):].split("/")[0].split("?")[0]
    try:
        fid = upage.verify_download_token(token)
    except upage.TokenError:
        await status.edit_text(
            "❌ That upload link is invalid or expired. Make a fresh "
            "one with /upage.")
        return "failed"
    rec = upage.lookup_file(fid)
    if rec is None:
        await status.edit_text(
            "❌ That upload link was already used or expired — the "
            "file is gone from the server. Upload it again with /upage.")
        return "failed"
    path, name, size = rec["path"], rec["name"], rec["size"]
    if not os.path.exists(path):
        upage.consume(fid)
        await status.edit_text(
            "❌ The file is no longer on the server. Upload it again "
            "with /upage.")
        return "failed"
    await status.edit_text(
        f"Uploading {name} ({human_bytes(size)}) from your web upload…")
    try:
        await send_files_routed(client, message.chat.id, [path],
                                "uploader", caption=name)
    except Exception as exc:
        await status.edit_text(
            f"❌ Telegram upload failed: {exc}\nThe file is still on "
            "the server — you can retry this link.")
        return "failed"
    upage.consume(fid)
    await status.edit_text(
        f"✅ {name} ({human_bytes(size)}) is now on Telegram, and the "
        "server copy is deleted.")
    return "done"


async def _dispatch(client: Client, message: Message, url: str,
                    status: Message, editor: ThrottledEditor) -> str:
    """Run the resolver chain.

    Returns "done" | "pending" | "failed" | "blocked" (bandwidth cap).
    """
    if stats.bandwidth_blocked():
        await status.edit_text(stats.bandwidth_pause_text())
        return "blocked"
    errors: list[str] = []
    try:
        # 0) Own web-upload links: the file is ALREADY on this disk.
        # Send it straight to Telegram — downloading it again would
        # waste disk, bandwidth and time for zero benefit.
        if config.BASE_URL:
            parsed = urlparse(url)
            base = urlparse(config.BASE_URL)
            if parsed.netloc == base.netloc \
                    and parsed.path.startswith("/u/"):
                return await _upage_local_flow(client, message, url,
                                               status)

        # 1) Terabox gets its own extractor, always first for its domains.
        if terabox.is_terabox_url(url):
            try:
                picker = await _terabox_flow(client, message, url,
                                             status, editor)
                return "pending" if picker else "done"
            except SizeBlocked as exc:
                await status.edit_text(f"❌ {exc}")
                return "failed"
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
                check_size(direct_info.get("size") or 0,
                           direct_info.get("filename") or "file")
            except SizeBlocked as exc:
                await status.edit_text(f"❌ {exc}")
                return "failed"
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
        except SizeBlocked as exc:
            await status.edit_text(f"❌ {exc}")
            return "failed"
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

        hint = ("\n\nIf it's Instagram/a login-walled site, it may need a "
                "cookies.txt — see the README.")
        if "reddit.com" in url and any(
                "blocked by network security" in e for e in errors):
            hint = ("\n\n⚠️ Reddit is blocking this server (\"blocked by "
                    "network security\"). Fix: log into reddit.com in your "
                    "browser, export a cookies.txt, and send it to me with "
                    "/setcookies as the caption — I'll browse as you from "
                    "then on.")
        if ("youtube.com" in url or "youtu.be" in url) and any(
                "not a bot" in e for e in errors):
            hint = ("\n\n⚠️ YouTube is challenging this server (\"not a "
                    "bot\"), cookies or not. Try in order: 1) /updateytdlp — "
                    "YouTube breaks old yt-dlp constantly; 2) re-export "
                    "fresh YouTube cookies while logged in — /setcookies → "
                    "YouTube warns if the file isn't a logged-in session; "
                    "3) cookies from a different Google account — YouTube "
                    "sometimes flags the account+IP combo.")
        await status.edit_text(
            "I couldn't download that with any engine:\n"
            + "\n".join(f"• {e}" for e in errors)
            + hint)
        return "failed"
    except Exception as exc:  # never leave a stuck status message
        try:
            await status.edit_text(f"Something went wrong: {exc}")
        except Exception:
            pass
        return "failed"


def _check_picked_format(job: dict, choice: str) -> None:
    """Refuse a quality-button tap when that format is too big.

    Raises SizeBlocked; the callback's except turns it into a
    "Download failed: …" message on the status line.
    """
    formats = job.get("formats") or []
    if not formats or choice == "audio":
        return  # audio-only size is unknown until the mp3 exists
    if choice == "best":
        picked = formats[0]
    else:
        picked = next((f for f in formats if f["format_id"] == choice), None)
    if picked:
        check_size(picked.get("filesize") or 0,
                   job.get("title") or "file")


# ---------------------------------------------------------------- callbacks

@Client.on_callback_query(filters.regex(r"^(fmt|tera):"))
async def format_callback(client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
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
    if stats.bandwidth_blocked():
        await query.answer("⛔ Bandwidth paused", show_alert=True)
        try:
            await query.message.edit_text(stats.bandwidth_pause_text())
        except Exception:
            pass
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
            if info:
                check_size(info.get("size") or 0, info.get("name") or "file")
            await _terabox_download(client, fake_message, job["url"], index,
                                    info, status, editor)
        else:
            choice = parts[2]
            _check_picked_format(job, choice)
            await _ytdlp_download(
                client, fake_message, job["url"], job.get("title", "Download"),
                status, editor,
                format_id=None if choice in ("best", "audio") else choice,
                audio_only=choice == "audio")
        if job.get("origin_chat") and job.get("origin_msg"):
            await react_to(client, job["origin_chat"], job["origin_msg"], "👍")
        stats.record_job("uploader", ok=True)
    except Exception as exc:
        if job.get("origin_chat") and job.get("origin_msg"):
            await react_to(client, job["origin_chat"], job["origin_msg"], "👎")
        stats.record_job("uploader", ok=False)
        try:
            await status.edit_text(f"Download failed: {exc}")
        except Exception:
            pass
