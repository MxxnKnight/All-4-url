""" /mediainfo — full metadata for a file, from a partial probe only.

Three ways to trigger it:
  1. Reply to any media message with /mediainfo
  2. Send a file with the caption `/mediainfo`
  3. Tap the 📊 MediaInfo button under a file-to-link / uploader result
     (callback data `mi:<chat_id>:<message_id>`)

The probe streams just the first bits of the file (see core/mediainfo),
so even a 2 GB video costs only a few MB of traffic. The full report is
published as a Telegraph page; the chat gets a short summary.
"""

import os

from pyrogram import Client, StopPropagation, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

from core import telegraph
from core.mediainfo import ProbeError, format_summary, probe_message
from core.utils import cleanup_dir, is_owner_id, make_job_dir, owner_only

MEDIA_FILTER = (filters.video | filters.document | filters.audio
                | filters.animation | filters.voice)


async def _caption_is_mediainfo(_, __, message: Message) -> bool:
    caption = (getattr(message, "caption", None) or "").strip()
    return caption == "/mediainfo" or caption.startswith("/mediainfo ")


caption_mediainfo = filters.create(_caption_is_mediainfo, "CaptionMediaInfo")


async def _probe_and_report(client: Client, chat_id: int, media_message,
                            status: Message | None = None) -> None:
    """Probe `media_message` and deliver summary + Telegraph link."""
    if status is None:
        status = await client.send_message(
            chat_id, "🔍 Probing — downloading just the first bits…")
    else:
        try:
            await status.edit_text(
                "🔍 Probing — downloading just the first bits…")
        except Exception:
            pass

    try:
        result = await probe_message(client, media_message)
    except ProbeError as exc:
        await status.edit_text(f"📊 MediaInfo failed: {exc}")
        return
    except Exception as exc:
        await status.edit_text(f"📊 MediaInfo failed unexpectedly: {exc}")
        return

    summary = format_summary(result)
    try:
        url = await telegraph.create_page(
            f"MediaInfo — {result.file_name}", result.text)
    except Exception:
        # Telegraph is a nicety, not a dependency: fall back to a file.
        job_dir = make_job_dir()
        path = os.path.join(job_dir, "mediainfo.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(result.text)
            await status.edit_text(
                summary + "\n\nTelegraph is unreachable right now, so the "
                "full report is attached as a file instead.")
            await client.send_document(chat_id, path,
                                       caption="Full MediaInfo report")
        finally:
            cleanup_dir(job_dir)
        return

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Full MediaInfo ↗", url=url)]])
    await status.edit_text(summary, reply_markup=markup)


@Client.on_message(filters.private & owner_only
                   & filters.command("mediainfo"))
async def mediainfo_cmd(client: Client, message: Message):
    target = message.reply_to_message
    if target is None or not (target.document or target.video or target.audio
                              or target.animation or target.voice):
        await message.reply_text(
            "Reply to a media message with /mediainfo and I'll probe it.\n"
            "You can also send a file with the caption /mediainfo, or tap "
            "the 📊 MediaInfo button under a result.")
        return
    await _probe_and_report(client, message.chat.id, target)


@Client.on_message(filters.private & owner_only & MEDIA_FILTER
                   & caption_mediainfo, group=-1)
async def mediainfo_caption(client: Client, message: Message):
    try:
        await _probe_and_report(client, message.chat.id, message)
    finally:
        # This message was a MediaInfo request, not a file-to-link upload:
        # stop the group-0 filetolink handler from also processing it.
        raise StopPropagation


@Client.on_callback_query(filters.regex(r"^mi:"))
async def mediainfo_callback(client: Client, query: CallbackQuery):
    if not is_owner_id(query.from_user.id if query.from_user else None):
        await query.answer("Not for you.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Stale button.")
        return
    try:
        chat_id, message_id = int(parts[1]), int(parts[2])
    except ValueError:
        await query.answer("Stale button.")
        return
    await query.answer("Probing…")
    try:
        target = await client.get_messages(chat_id, message_id)
    except Exception:
        target = None
    if target is None or getattr(target, "empty", False):
        await query.answer("That message is gone — links die with it.",
                           show_alert=True)
        return
    chat = query.message.chat.id if query.message else chat_id
    await _probe_and_report(client, chat, target)
