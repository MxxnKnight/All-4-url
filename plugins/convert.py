""" /convert — change a file's Telegram type, without re-uploading.

Reply to a video/document/animation message with /convert and the
bot replies with buttons: Convert to video / Convert to document
(only the other type is offered). Pressing one re-sends the SAME
file, by its Telegram file_id, as the other type — no download,
no re-upload from the server, no bandwidth counted, and the
original message is untouched.

Limits, honestly:
  * Convert to document always works — any file can be a document.
  * Convert to video only works if the file itself IS a video
    (an mp4 sent as a document, a video sent as a document, …).
    Converting a PDF, a zip, or an mkv to a video will be refused
    by Telegram, and the bot will say so instead of pretending.
  * A converted video may lose its duration/thumbnail when the
    original was a document (Telegram never stored video metadata
    for it) — it still plays as a video, just without the nice
    preview numbers. Converting a video to a document loses
    nothing: the file bytes are identical.
  * This is the same conversion the file menu's 🔄 Convert button
    uses — the menu button just opens this sub-menu in place.
"""

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import filemenu, stats
from core.utils import admin_only, human_bytes, is_admin_id


def _media_and_kind(message):
    for kind in ("video", "document", "animation"):
        media = getattr(message, kind, None)
        if media is not None:
            return kind, media
    return None, None


@Client.on_message(filters.private & admin_only
                   & filters.command("convert"))
async def convert_cmd(client: Client, message):
    replied = message.reply_to_message
    if replied is None:
        await message.reply_text(
            "Reply to a video or document with /convert — send the "
            "file first, then reply to that message with this command.")
        return
    kind, media = _media_and_kind(replied)
    if kind is None:
        await message.reply_text(
            "Convert only works for video and document files. Reply "
            "to a video or a document with /convert.")
        return
    name, size, _mime, _k, _v, _z = filemenu.file_info(replied)
    text, markup = filemenu.convert_menu_markup(
        replied.chat.id, replied.id, kind)
    await message.reply_text(
        f"🔄 **{name}** ({human_bytes(size)})\n\n{text}",
        reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^cv:"))
async def convert_callback(client: Client, query):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Stale button.")
        return
    try:
        chat_id, msg_id = int(parts[1]), int(parts[2])
    except ValueError:
        await query.answer("Stale button.")
        return
    target_kind = parts[3]
    if target_kind not in ("vid", "doc"):
        await query.answer("Stale button.")
        return
    await query.answer("Converting…")
    try:
        target = await client.get_messages(chat_id, msg_id)
    except Exception:
        target = None
    if target is None or getattr(target, "empty", False):
        await query.answer("That file message is gone.",
                           show_alert=True)
        return
    kind, media = _media_and_kind(target)
    if kind is None:
        await query.answer("That message has no convertible file.",
                           show_alert=True)
        return
    file_id = getattr(media, "file_id", None)
    if not file_id:
        await query.answer("That file has no id to convert.",
                           show_alert=True)
        return
    name, _size, _mime, _k, _v, _z = filemenu.file_info(target)
    dest = query.message.chat.id if query.message else chat_id
    try:
        if target_kind == "vid":
            sent = await client.send_video(
                dest, file_id, caption=name, supports_streaming=True,
                duration=getattr(media, "duration", 0) or 0,
                width=getattr(media, "width", 0) or 0,
                height=getattr(media, "height", 0) or 0)
            label = "video"
        else:
            sent = await client.send_document(dest, file_id, caption=name)
            label = "document"
    except Exception as exc:
        # Telegram refusing a video conversion is the expected
        # failure (non-video file, mkv, …) — say so plainly, and
        # keep the original file untouched.
        try:
            await query.message.edit_text(
                f"❌ Couldn't convert {name} to a {label}: {exc}\n"
                f"The original file is untouched.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "❌ Close",
                        callback_data=f"fm:{chat_id}:{msg_id}:close")]]))
        except Exception:
            pass
        stats.record_job("convert", ok=False)
        return
    if sent is None:
        try:
            await query.message.edit_text(
                f"❌ Conversion returned nothing for {name}. "
                f"The original file is untouched.")
        except Exception:
            pass
        stats.record_job("convert", ok=False)
        return
    stats.record_job("convert", ok=True)
    try:
        await query.message.edit_text(
            f"✅ {name} is now a {label} (sent above). "
            f"The original file is untouched.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "❌ Close",
                    callback_data=f"fm:{chat_id}:{msg_id}:close")]]))
    except Exception:
        pass
