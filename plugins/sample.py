"""🎬 Sample video from a Telegram video, via the file menu.

Callback: ``fp:<chat>:<msg>:<seconds>`` (15, 30, 60, chosen in the
sample sub-menu). The bot downloads the video to disk (refusing
plainly if it cannot fit), cuts a clip from the middle with
ffmpeg (copy first, re-encode fallback), sends it as a video,
then deletes everything. The menu message is edited to a success
note with Back / Close buttons.
"""

import os

from pyrogram import Client, filters

from core import filemenu, stats, videotools
from core.sender import send_files_routed
from core.utils import (admin_only, cleanup_dir, human_bytes, is_admin_id,
                        make_job_dir, run_blocking)


@Client.on_callback_query(filters.regex(r"^fp:"))
async def sample_callback(client: Client, query):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Stale button.")
        return
    try:
        chat_id, msg_id, seconds = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await query.answer("Stale button.")
        return
    if seconds not in (15, 30, 60):
        await query.answer("Stale button.")
        return
    await query.answer(f"Cutting a {seconds}s sample…")
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
        await query.answer("Samples only work for videos.",
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
                f"❌ Can't sample this one: {exc}",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
            return
        await status.edit_text(f"🎬 Downloading {name} for a sample…")
        video_path = await client.download_media(
            target, file_name=os.path.join(job_dir, "video"))
        if video_path is None or not os.path.exists(video_path):
            await status.edit_text(
                "❌ Couldn't download that video back.",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
            return
        duration = await run_blocking(videotools.duration_of, video_path)
        await status.edit_text(
            f"🎬 Cutting a {seconds}s sample from the middle of "
            f"{name}… (re-encode can take a minute)")
        sample_path = os.path.join(job_dir, "sample.mp4")
        try:
            await run_blocking(
                videotools.make_sample, video_path, duration, seconds,
                sample_path)
        except Exception as exc:
            await status.edit_text(
                f"❌ Sample failed: {exc}",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
            stats.record_job("sample", ok=False)
            return
        await status.edit_text(f"🎬 Sending your {seconds}s sample…")
        await send_files_routed(
            client, dest, [sample_path], "uploader",
            caption=f"Sample ({seconds}s, middle) of {name}",
            status_msg=status)
        stats.record_job("sample", ok=True)
        await status.edit_text(
            f"✅ Sent a {seconds}s sample of {name}.",
            reply_markup=filemenu.back_close_markup(chat_id, msg_id))
    except Exception as exc:
        try:
            await status.edit_text(
                f"❌ Sample failed: {exc}",
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
        except Exception:
            pass
        stats.record_job("sample", ok=False)
    finally:
        cleanup_dir(job_dir)
