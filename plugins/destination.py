"""Upload destination picker (owner only): /destination

Choose where finished files are delivered, separately for URL uploads
and restricted downloads:

  💬 Bot PM         — the bot sends the files to your chat with it
  💾 Saved Messages — your user account uploads them into your own
                      Saved Messages (Destiny-style; needs SESSION_STRING)

Stored via core/settings.py (Mongo when DB_URI is set, else
data/settings.json) — survives redeploys either way.
"""

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from core import settings
from core.utils import is_owner_id, owner_only

_FEATURES = (("uploader", "🔗 URL uploads"),
             ("restricted", "📥 Restricted downloads"))
_DEST_LABEL = {"bot": "💬 Bot PM", "saved": "💾 Saved Messages"}


def _text_and_markup():
    lines = ["📤 **Upload destination**",
             "",
             "Where finished files land. **Saved Messages** delivers "
             "into your own account's Saved Messages via the user "
             "session — handy as a private vault, and uploads there "
             "don't clutter this chat.",
             ""]
    rows = []
    for feature, label in _FEATURES:
        current = settings.get_dest(feature)
        lines.append(f"{label}: **{_DEST_LABEL[current]}**")
        if current == "saved" and not config.SESSION_STRING:
            lines.append("   ⚠️ No SESSION_STRING set — uploads will "
                         "fall back to Bot PM until you add one.")
        row = []
        for dest in ("bot", "saved"):
            mark = " ✓" if dest == current else ""
            row.append(InlineKeyboardButton(
                f"{_DEST_LABEL[dest]}{mark}",
                callback_data=f"dest:{feature}:{dest}"))
        rows.append([InlineKeyboardButton(f"— {label} —",
                                          callback_data="dest:noop")])
        rows.append(row)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@Client.on_message(filters.private & owner_only
                   & filters.command("destination"))
async def destination_cmd(_client: Client, message: Message):
    text, markup = _text_and_markup()
    await message.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^dest:"))
async def destination_callback(_client: Client, query: CallbackQuery):
    if not is_owner_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) == 3 and parts[1] in ("uploader", "restricted"):
        settings.set_dest(parts[1], parts[2])
        text, markup = _text_and_markup()
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        await query.answer("Destination updated ✓")
    else:
        await query.answer()
