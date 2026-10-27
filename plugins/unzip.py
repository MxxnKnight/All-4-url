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

Limits (same as the URL prompt): zip ≤ 1 GB, extracted total
≤ 8 GB, ≤ 50 files, password-protected entries skipped, unsafe
paths and symlinks skipped, nested zips extracted as files
(not recursed). Files are extracted, uploaded, and deleted
ONE AT A TIME, so disk only ever holds the zip plus one file —
a zip that expands to several GB can still go through, as
long as each single file fits Telegram's 2 GB cap and the
largest file fits free disk. Anything over a cap gets a plain
"exceeds" message and nothing is extracted.
"""

import os

from pyrogram import Client, filters
from pyrogram.types import Message

from core import stats, unzip
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
            unzip.check_disk_room(zip_path, info["largest"])
        except unzip.ZipTooBig as exc:
            await status.edit_text(f"📦 Can't unzip this one: {exc}")
            return
        except unzip.ZipBad as exc:
            await status.edit_text(f"❌ {exc}")
            return
        await status.edit_text(
            f"Unzipping {info['count']} file(s) "
            f"({human_bytes(info['total'])}) — one by one…")
        # One by one: extract one file, upload it, delete it,
        # then extract the next. Disk never holds more than
        # the zip plus one file, so a multi-GB total fits.
        skipped = list(info["skipped"]) + info["encrypted"] \
            + info["too_big"]
        failed: list[str] = []
        sent_count = 0
        sent_bytes = 0
        bandwidth_stopped = False
        gen = unzip.iter_extracted(
            zip_path, os.path.join(job_dir, "extracted"), skipped)
        try:
            for path in gen:
                if stats.bandwidth_blocked():
                    bandwidth_stopped = True
                    break
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                try:
                    await status.edit_text(
                        f"Uploading {sent_count + 1} of "
                        f"{info['count']}: {os.path.basename(path)}…")
                except Exception:
                    pass
                try:
                    await send_files_routed(
                        client, message.chat.id, [path],
                        "uploader", caption=f"Unzipped from {name}",
                        status_msg=status)
                    sent_count += 1
                    sent_bytes += size
                except Exception as exc:
                    failed.append(f"{os.path.basename(path)} ({exc})")
                try:
                    os.unlink(path)
                except OSError:
                    pass
        finally:
            gen.close()
        if sent_count == 0 and not bandwidth_stopped:
            await status.edit_text(
                "❌ Nothing could be unzipped from this file"
                + (f" (skipped: {', '.join(skipped[:5])})"
                   if skipped else "")
                + ".")
            return
        summary = (f"✅ Unzipped {sent_count} file(s) "
                   f"({human_bytes(sent_bytes)}) from {name}.")
        if bandwidth_stopped:
            summary += ("\n⚠️ Stopped early: monthly bandwidth cap "
                        "reached — remaining files were not sent.")
        if failed:
            summary += "\n⚠️ Failed to send: " + ", ".join(failed[:5])
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
