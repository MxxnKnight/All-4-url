""" /phost — host a replied image on Catbox, get a direct image URL.

Reply /phost to a photo or an image sent as a file. The bot downloads
it from Telegram, uploads it to Catbox (free, no account, no API key),
replies with the direct link (files.catbox.moe/….jpg — displays as an
image anywhere), and deletes its temp copy.

Deliberately separate from file-to-link: sending an image does NOT
auto-produce download/stream links (see _is_image_message in
plugins/filetolink.py) — images stay quiet until /phost is replied to
one. That separation is the point: an image is not a file waiting for
a download link, it's a picture waiting for a host.

Caveats (told to the owner, not hidden): Catbox links are public,
have no password and no expiry, and anonymous uploads can't be
revoked — don't host anything private. A free host can also go down
or prune files, so these links are convenient, not archival. And a
replied photo is Telegram's compressed JPG, not the original bytes —
send the image as a file for the original.
"""

import os
import shutil
import tempfile

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.utils import admin_only, human_bytes

#: Catbox's anonymous upload endpoint (no key, no account).
CATBOX_API_URL = "https://catbox.moe/user/api.php"
#: Catbox's anonymous per-file limit.
CATBOX_MAX_BYTES = 200 * 1024 * 1024


def _image_target(message):
    """(size, filename) for a /phost-able reply, else None.

    Accepts a photo (Telegram's compressed JPG) or a document whose
    MIME type is an image (the original file, sent as a file)."""
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None
    if getattr(reply, "photo", None) is not None:
        return (getattr(reply.photo, "file_size", 0) or 0, "image.jpg")
    doc = getattr(reply, "document", None)
    if doc is not None and (doc.mime_type or "").startswith("image/"):
        name = doc.file_name or None
        if not name:
            subtype = (doc.mime_type or "image/jpeg").split("/", 1)[-1]
            name = f"image.{subtype}"
        return (doc.file_size or 0, name)
    return None


async def upload_to_catbox(path: str, filename: str,
                           api_url: str | None = None) -> str:
    """Upload one file to Catbox; returns the direct URL or raises.

    The file streams from disk (never loaded into RAM). `api_url`
    defaults to Catbox at call time (not import time, so tests and
    overrides actually take effect); pass a URL only to point this at
    a fake server in tests.
    """
    if api_url is None:
        api_url = CATBOX_API_URL
    timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_read=120)
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with open(path, "rb") as fh:
            form.add_field("fileToUpload", fh, filename=filename)
            async with session.post(api_url, data=form) as resp:
                status = resp.status
                text = (await resp.text()).strip()
    if status != 200 or not text.startswith("https://"):
        raise RuntimeError(
            f"Catbox refused the upload ({status}): {text[:200]}")
    return text


@Client.on_message(filters.private & admin_only
                   & filters.command("phost"))
async def phost_cmd(client: Client, message):
    target = _image_target(message)
    if target is None:
        await message.reply_text(
            "Reply /phost to an image — a photo, or an image sent as a "
            "file — and I'll host it and give you a direct link.")
        return
    size, filename = target
    if size > CATBOX_MAX_BYTES:
        await message.reply_text(
            f"That image is {human_bytes(size)} — Catbox only takes "
            "files up to 200 MB.")
        return
    status = await message.reply_text("Uploading to Catbox…")
    tmpdir = tempfile.mkdtemp(prefix="phost_")
    try:
        path = await client.download_media(
            message.reply_to_message,
            file_name=os.path.join(tmpdir, filename))
        url = await upload_to_catbox(path, filename)
    except Exception as exc:
        await status.edit_text(f"❌ Hosting failed: {exc}")
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    await status.edit_text(
        f"🖼 Direct image link:\n{url}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🖼 Open image", url=url)]]))
