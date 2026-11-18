""" /unzip — unpack an archive file the owner sent, then upload contents.

Usage: send a .zip/.rar/.7z file to the bot, then reply to that
message with /unzip. The bot downloads that archive back from
Telegram, pre-checks its total uncompressed size from the
archive headers (never extracts first), unpacks it safely, and
uploads each extracted file to this chat.

This is the reply-command half of the unpack feature; the other
half is the prompt plugins/uploader.py shows when a URL download
turns out to be an archive. Both share core/unzip.py, so the
caps and safety guards are identical.

Limits (same as the URL prompt): archive ≤ 20 GB, extracted
total ≤ 20 GB, ≤ 1000 files, password-protected entries
skipped, unsafe paths and symlinks skipped, nested archives
extracted as files (not recursed). Files are extracted,
uploaded, and deleted ONE AT A TIME, so disk only ever holds
the archive plus one file — an archive that expands to several
GB can still go through, as long as each single file fits
Telegram's 2 GB cap (bigger ones arrive split into .001/.002
parts) and the largest file fits free disk. Anything over a
cap gets a plain "exceeds" message and nothing is extracted.
"""

import asyncio
import os

from pyrogram import Client, filters
from pyrogram.types import Message

from core import stats, unzip
from core.sender import send_files_routed, send_files_split
from core.utils import (admin_only, cleanup_dir, human_bytes, make_job_dir)


@Client.on_message(filters.private & admin_only
                   & filters.command("unzip"))
async def unzip_cmd(client: Client, message: Message):
    replied = message.reply_to_message
    if replied is None:
        await message.reply_text(
            "Reply to an archive file with /unzip — send the archive first, "
            "then reply to that message with this command.")
        return
    doc = getattr(replied, "document", None)
    if doc is None:
        await message.reply_text(
            "That message has no file in it. Reply to an archive "
            "file with /unzip.")
        return
    name = doc.file_name or "file"
    if not name.lower().endswith(tuple(unzip.ARCHIVE_EXTS)):
        await message.reply_text(
            f"That file ({name}) is not an archive. Reply to a "
            f".zip, .rar or .7z file with /unzip.")
        return
    if doc.file_size and doc.file_size > unzip.MAX_ZIP_BYTES:
        await message.reply_text(
            f"📦 This archive is {human_bytes(doc.file_size)} — over the "
            f"{human_bytes(unzip.MAX_ZIP_BYTES)} unpack limit, so I "
            f"can't unpack it on this server.")
        return
    status = await message.reply_text("Downloading that archive back…")
    job_dir = make_job_dir()
    try:
        ext = os.path.splitext(name)[1].lower() or ".zip"
        zip_path = await client.download_media(
            replied, file_name=os.path.join(job_dir, "input" + ext))
        if zip_path is None or not os.path.exists(zip_path):
            await status.edit_text("❌ Couldn't download that archive back.")
            return
        if not unzip.is_archive_file(zip_path):
            await status.edit_text("❌ That file isn't actually an archive.")
            return
        try:
            info = unzip.archive_info(zip_path)
            unzip.check_disk_room(zip_path, info["largest"])
        except unzip.ZipTooBig as exc:
            await status.edit_text(f"📦 Can't unpack this one: {exc}")
            return
        except unzip.ZipBad as exc:
            await status.edit_text(f"❌ {exc}")
            return
        await status.edit_text(
            f"Unpacking {info['count']} file(s) "
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
                # Oversize files are split into .001/.002 parts
                # before sending (v1.40.0) — a 10 GB video inside
                # the zip arrives as mkv.001, mkv.002, … instead
                # of failing the cap.
                sent_paths = [path]
                try:
                    sent_paths = await send_files_split(
                        client, message.chat.id, [path],
                        "uploader", caption=f"Unpacked from {name}",
                        status_msg=status)
                    sent_count += 1
                    sent_bytes += size
                except Exception as exc:
                    failed.append(f"{os.path.basename(path)} ({exc})")
                for sent in sent_paths:
                    try:
                        os.unlink(sent)
                    except OSError:
                        pass
                # Pace one-by-one sends: 65+ tiny files in a row
                # would otherwise hit Telegram's FloodWait. 1s per
                # file is cheap (65 files = ~1 extra minute) and
                # keeps a many-file job from failing halfway.
                await asyncio.sleep(1)
        finally:
            gen.close()
        if sent_count == 0 and not bandwidth_stopped:
            await status.edit_text(
                "❌ Nothing could be unpacked from this file"
                + (f" (skipped: {', '.join(skipped[:5])})"
                   if skipped else "")
                + ".")
            return
        summary = (f"✅ Unpacked {sent_count} file(s) "
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
            await status.edit_text(f"❌ Unpack failed: {exc}")
        except Exception:
            pass
    finally:
        cleanup_dir(job_dir)
