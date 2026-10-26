""" /unzip — unzip a zip file the owner sent, then upload contents.

Usage: send a zip file to the bot, then reply to that message
with /unzip. The bot downloads that zip back from Telegram,
pre-checks its total uncompressed size from the zip headers
(never extracts first), unzips it safely, and uploads each
extracted file to this chat.

This is the reply-command half of the unzip feature; the other
half is the prompt plugins/uploader.py shows when a URL download
turns out to be a zip. Both share core/unzip.py, so the caps
and safety guards are identical.

Limits (same as the URL prompt): zip ≤ 500 MB, extracted total
≤ 1 GB, ≤ 50 files, password-protected entries skipped, unsafe
paths and symlinks skipped, nested zips extracted as files
(not recursed). Anything over a cap gets a plain "exceeds"
message and nothing is extracted.
"""

import os

from pyrogram import Client, filters
from pyrogram.types import Message

from core import unzip
from core.sender import send_files_routed
from core.utils import (admin_only, cleanup_dir, human_bytes, make_job_dir)


@Client.on_message(filters.private & admin_only
                   & filters.command("unzip"))
async def unzip_cmd(client: Client, message: Message):
    replied = message.reply_to_message
    if replied is None:
        await message.reply_text(
            "Reply to a zip file with /unzip — send the zip first, "
            "then reply to that message with this command.")
        return
    doc = getattr(replied, "document", None)
    if doc is None:
        await message.reply_text(
            "That message has no file in it. Reply to a zip file "
            "with /unzip.")
        return
    name = doc.file_name or "file"
    if not name.lower().endswith(".zip"):
        await message.reply_text(
            f"That file ({name}) is not a zip. Reply to a .zip file "
            f"with /unzip.")
        return
    if doc.file_size and doc.file_size > unzip.MAX_ZIP_BYTES:
        await message.reply_text(
            f"📦 This zip is {human_bytes(doc.file_size)} — over the "
            f"{human_bytes(unzip.MAX_ZIP_BYTES)} unzip limit, so I "
            f"can't unzip it on this server.")
        return
    status = await message.reply_text("Downloading that zip back…")
    job_dir = make_job_dir()
    try:
        zip_path = await client.download_media(
            replied, file_name=os.path.join(job_dir, "input.zip"))
        if zip_path is None or not os.path.exists(zip_path):
            await status.edit_text("❌ Couldn't download that zip back.")
            return
        if not unzip.is_zip_file(zip_path):
            await status.edit_text("❌ That file isn't actually a zip.")
            return
        try:
            info = unzip.zip_info(zip_path)
            unzip.check_disk_room(zip_path, info["total"])
        except unzip.ZipTooBig as exc:
            await status.edit_text(f"📦 Can't unzip this one: {exc}")
            return
        except unzip.ZipBad as exc:
            await status.edit_text(f"❌ {exc}")
            return
        await status.edit_text(
            f"Unzipping {info['count']} file(s) "
            f"({human_bytes(info['total'])})…")
        try:
            extracted, skipped = unzip.extract_zip(
                zip_path, os.path.join(job_dir, "extracted"))
        except unzip.ZipTooBig as exc:
            await status.edit_text(f"📦 Can't unzip this one: {exc}")
            return
        except Exception as exc:
            await status.edit_text(f"❌ Unzip failed: {exc}")
            return
        if not extracted:
            await status.edit_text(
                "❌ Nothing could be unzipped from this file"
                + (f" (skipped: {', '.join(skipped[:5])})"
                   if skipped else "")
                + ".")
            return
        await status.edit_text(f"Uploading {len(extracted)} file(s)…")
        await send_files_routed(client, message.chat.id, extracted,
                                "uploader", caption=f"Unzipped from {name}",
                                status_msg=status)
        summary = (f"✅ Unzipped {len(extracted)} file(s) "
                   f"({human_bytes(info['total'])}) from {name}.")
        if skipped:
            summary += "\n⚠️ Skipped: " + ", ".join(skipped[:10])
        await status.edit_text(summary)
    except Exception as exc:
        try:
            await status.edit_text(f"❌ Unzip failed: {exc}")
        except Exception:
            pass
    finally:
        cleanup_dir(job_dir)
