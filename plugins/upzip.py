""" /upzip — collect links into one zip (v1.51.16).

/upzip opens a collection session (core/collect): every link the
owner sends afterwards is downloaded and appended to one zip
instead of being sent to him, until he taps 📦 Send as zip (on
the page or in the chat status message) or 🗑 Discard. Sending
/upzip again while a session runs just re-shows the current one.
"""

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from core import collect
from core.utils import admin_only, is_admin_id


@Client.on_message(filters.command("upzip") & filters.private
                   & admin_only)
async def upzip_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    session = collect.active(user_id)
    if session is not None:
        await collect.reshow(client, session)
        return
    await collect.start(client, message.chat.id, user_id)


@Client.on_callback_query(filters.regex(r"^collect:"))
async def collect_callback(client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) < 2:
        await query.answer("Stale button.")
        return
    session = collect.active(query.from_user.id
                             if query.from_user else None)
    if session is None:
        await query.answer("No collection running — send /upzip first.",
                           show_alert=True)
        return
    if parts[1] == "send":
        await query.answer("Sending the zip…")
        result = await collect.send_zip(client, session)
        if result == "already":
            await query.answer("Already sending…")
        elif result.startswith("failed"):
            await query.answer(result, show_alert=True)
    elif parts[1] == "discard":
        await query.answer("Discarding…")
        await collect.discard(client, session)
    else:
        await query.answer("Stale button.")
