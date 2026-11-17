""" /live <link> — record a live stream into Telegram, part by part.

    /live <link to a live stream>

The bot checks the link is live right now (yt-dlp), then ffmpeg
records it — stream-copy, no re-encode — into 15-minute MP4 parts
(core/live.py). Each finished part is sent here as a video the
moment it closes, then deleted from the server disk. A ⏹ Stop
button on the status message ends the recording (the part in
progress is finished and sent too); a stream ending on its own
ends it the same way. One recording at a time.
"""

import asyncio

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from core import live, stats
from core.utils import admin_only, is_admin_id, react

USAGE = (
    "Usage: `/live <link to a live stream>`\n\n"
    "Records a live stream and sends it here in 15-minute video "
    "parts, as each part finishes — nothing stays on the server "
    "disk. Tap ⏹ Stop on the status message to end the recording.\n\n"
    "• Works on live streams yt-dlp can read (YouTube Live, "
    "Twitch, Kick, Stripchat, …)\n"
    "• One recording at a time\n"
    "• The monthly bandwidth cap still applies — the recording "
    "stops if it's reached\n"
    "• A server restart ends the recording (nothing is saved)"
)

_BUSY_TEXT = (
    "🔴 Already recording a live stream — one at a time. "
    "Stop it with the ⏹ Stop button on its status message first."
)


@Client.on_message(filters.command("live") & filters.private & admin_only)
async def live_cmd(client: Client, message: Message):
    if stats.bandwidth_blocked():
        await message.reply_text(stats.bandwidth_pause_text())
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text(USAGE)
        return
    url = parts[1].strip()
    if live.busy():
        await message.reply_text(_BUSY_TEXT)
        return
    await react(message, "🔴")
    status = await message.reply_text("🔍 Checking the link…")
    # The real gate: atomic — a second /live landing between the
    # busy() check above and this line still loses here.
    if not await live.acquire_slot():
        await status.edit_text(_BUSY_TEXT)
        return
    # Fire and forget: the recording runs for hours; the job
    # releases the slot and cleans up when it ends.
    asyncio.create_task(live.run_live_job(client, message, url, status))


@Client.on_callback_query(filters.regex(r"^lv:"))
async def live_callback(_client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] != "stop":
        await query.answer("Stale button.")
        return
    job = live.get_job(parts[2])
    if job is None:
        await query.answer("That recording already ended.")
        return
    if job.stop_requested.is_set():
        await query.answer("Already stopping…")
        return
    job.request_stop()
    await query.answer("Stopping — the last part will be sent shortly.")
