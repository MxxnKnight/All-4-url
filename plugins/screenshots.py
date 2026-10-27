"""📸 Screenshots from a Telegram video, via the file menu.

Callback: ``fs:<chat>:<msg>:<count>`` (count = 1, 3, 5, 10, chosen
in the screenshot sub-menu). The bot downloads the video to disk
(refusing plainly if it cannot fit), takes evenly spaced JPEG
screenshots with ffmpeg, sends them as photos, then deletes
everything. The menu message is edited to a success note with
Back / Close buttons, so the user can do another action on the
same file without re-sending it.
"""

import os

from pyrogram import Client, filters

from core import filemenu, stats, videotools
from core.sender import send_files_routed
from core.utils import (admin_only, cleanup_dir, human_bytes, is_admin_id,
                        make_job_dir, run_blocking)


@Client.on_callback_query(filters.regex(r"^fs:"))
async def screenshot_callback(client: Client, query):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Stale button.")
        return
    try:
        chat_id, msg_id, count = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await query.answer("Stale button.")
        return
    if count not in (1, 3, 5, 10):
        await query.answer("Stale button.")
        return
    await query.answer(f"Taking {count} screenshot(s)…")
    try:
        target = await client.get_messages(chat_id, msg_id)
    except Exception:
        target = None
    if target is None or getattr(target, "empty", False):
        await query.answer("That file message is gone.",
                           show_alert=True)
        return
    name, size, _mime, _kind, is_video, _zip = filemenu.file_info(target)
    if not is_video:
        await query.answer("Screenshots only work for videos.",
                           show_alert=True)
        return
    dest = query.message.chat.id if query.message else chat_id
    status = query.message
    job_dir = make_job_dir()
    try:
        try:
            videotools.check_disk_for(size, job_dir)
        except ValueError as exc:
            await status.edit_text(
                f"❌ Can't screenshot this one: {exc}",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
            return
        await status.edit_text(
            f"📸 Downloading {name} for screenshots…")
        video_path = await client.download_media(
            target, file_name=os.path.join(job_dir, "video"))
        if video_path is None or not os.path.exists(video_path):
            await status.edit_text(
                "❌ Couldn't download that video back.",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
            return
        duration = await run_blocking(videotools.duration_of, video_path)
        await status.edit_text(
            f"📸 Taking {count} screenshot(s) from {name}…")
        shots = await run_blocking(
            videotools.make_screenshots, video_path, duration, count,
            os.path.join(job_dir, "shots"))
        if not shots:
            await status.edit_text(
                "❌ Couldn't take any screenshots from this video.",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
            stats.record_job("screenshots", ok=False)
            return
        await status.edit_text(f"📸 Sending {len(shots)} screenshot(s)…")
        await send_files_routed(
            client, dest, shots, "uploader",
            caption=f"Screenshots from {name}", status_msg=status)
        stats.record_job("screenshots", ok=True)
        await status.edit_text(
            f"✅ Sent {len(shots)} screenshot(s) from {name}.",
            reply_markup=filemenu.back_close_markup(chat_id, msg_id))
    except Exception as exc:
        try:
            await status.edit_text(
                f"❌ Screenshots failed: {exc}",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
        except Exception:
            pass
        stats.record_job("screenshots", ok=False)
    finally:
        cleanup_dir(job_dir)
