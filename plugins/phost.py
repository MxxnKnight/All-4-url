""" /phost — host a replied image, get a direct image URL.

Reply /phost to a photo or an image sent as a file. The bot downloads
it from Telegram, uploads it, replies with the direct link (displays as
an image anywhere), and deletes its temp copy.

Two anonymous hosts are tried in order, because a free host can refuse
the bot's datacenter IP even when it works from a home connection:

  1. Catbox (files.catbox.moe) — no account, no key, 200 MB per file.
  2. freeimage.host (iili.io links) — Chevereto API v1, same idea; its
     public API key can be overridden with the FREEIMAGE_KEY env var.

When the first host refuses, the second is used and the reply says
which one served the file; if both refuse, the message lists each
host's own error instead of one vague failure.

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

import json
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

#: Fallback image host (Chevereto v1 API). The key below is
#: freeimage.host's own public API key — not a private credential — and
#: FREEIMAGE_KEY overrides it if the owner ever wants a different one.
FREEIMAGE_API_URL = "https://freeimage.host/api/1/upload"
FREEIMAGE_PUBLIC_KEY = "6d207e02198a847aa98d0a2a901485a5"
FREEIMAGE_MAX_BYTES = 64 * 1024 * 1024


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


async def upload_to_freeimage(path: str, filename: str,
                              api_url: str | None = None,
                              key: str | None = None) -> str:
    """Upload one image to freeimage.host; returns the direct URL.

    Same streaming upload as Catbox (the file is never read into RAM).
    The API answers JSON: {status_code: 200, image: {url: "https://iili.io/…"}}.
    """
    if api_url is None:
        api_url = FREEIMAGE_API_URL
    if key is None:
        key = (os.environ.get("FREEIMAGE_KEY") or "").strip() \
            or FREEIMAGE_PUBLIC_KEY
    timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_read=120)
    form = aiohttp.FormData()
    form.add_field("key", key)
    form.add_field("action", "upload")
    form.add_field("format", "json")
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with open(path, "rb") as fh:
            form.add_field("source", fh, filename=filename)
            async with session.post(api_url, data=form) as resp:
                status = resp.status
                text = (await resp.text()).strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError(
            f"freeimage.host answered with a non-JSON page ({status}): "
            f"{text[:200]}")
    image = data.get("image") or {}
    url = (image.get("url") or "").strip()
    success = data.get("status_code") == 200 and data.get("success")
    if not success or not url.startswith("http"):
        reason = ((data.get("error") or {}).get("message")
                  if isinstance(data.get("error"), dict) else None) \
            or data.get("status_txt") or text[:200]
        raise RuntimeError(
            f"freeimage.host refused the upload ({status}): {reason}")
    return url


async def upload_image(path: str, filename: str) -> tuple[str, str]:
    """Upload to the first host that accepts it; returns (url, host name)."""
    errors: list[str] = []
    for label, upload in (("Catbox", upload_to_catbox),
                          ("freeimage.host", upload_to_freeimage)):
        try:
            return await upload(path, filename), label
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(" · ".join(errors))


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
            f"That image is {human_bytes(size)} — the free hosts take "
            f"up to {human_bytes(CATBOX_MAX_BYTES)}.")
        return
    status = await message.reply_text("Uploading the image…")
    tmpdir = tempfile.mkdtemp(prefix="phost_")
    try:
        path = await client.download_media(
            message.reply_to_message,
            file_name=os.path.join(tmpdir, filename))
        if not path:
            raise RuntimeError("Telegram gave me no file to upload")
        url, host = await upload_image(path, filename)
    except Exception as exc:
        await status.edit_text(f"❌ Hosting failed: {exc}")
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    await status.edit_text(
        f"🖼 Direct image link ({host}):\n{url}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🖼 Open image", url=url)]]))
