"""Upload destination picker (every admin): /destination

Choose where finished files are delivered, separately for URL uploads
and restricted downloads:

  💬 Bot PM         — the bot sends the files to your chat with it
  💾 Saved Messages — your user account uploads them into your own
                      Saved Messages (Destiny-style; needs SESSION_STRING)

And how URL uploads arrive — video files as 🎬 Video (inline player)
or 📄 Document (plain file). Restricted downloads always send videos
as video; the setting is URL uploads only.

Per-user (v1.45.0): every admin — the owner and each sudo user — has
their OWN settings. Changing yours never touches anyone else's, and
someone who never opened /destination follows the shared values.
Saved Messages is owner-only: the vault belongs to the single user
session (the owner's account), so sudo users only ever see Bot PM.

Stored via core/settings.py (Mongo when DB_URI is set, else
data/settings.json) — survives redeploys either way.
"""

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from core import backup, settings
from core.utils import admin_only, is_admin_id

_FEATURES = (("uploader", "🔗 URL uploads"),
             ("restricted", "📥 Restricted downloads"))
_DEST_LABEL = {"bot": "💬 Bot PM", "saved": "💾 Saved Messages"}
_SENDAS_LABEL = {"video": "🎬 Video", "document": "📄 Document"}


def _text_and_markup(user_id: int):
    is_owner = user_id == config.OWNER_ID
    lines = ["📤 **Your upload settings**",
             "",
             "Where YOUR finished files land — each person has their "
             "own settings here, so changing yours never touches "
             "anyone else's.",
             ""]
    if not is_owner:
        lines.append("💾 Saved Messages belongs to the owner's account, "
                     "so your files always come to this chat.")
        lines.append("")
    rows = []
    for feature, label in _FEATURES:
        current = settings.get_dest(feature, user_id)
        lines.append(f"{label}: **{_DEST_LABEL[current]}**")
        if current == "saved" and not config.SESSION_STRING:
            lines.append("   ⚠️ No SESSION_STRING set — uploads will "
                         "fall back to Bot PM until you add one.")
        dests = ("bot", "saved") if is_owner else ("bot",)
        row = []
        for dest in dests:
            mark = " ✓" if dest == current else ""
            row.append(InlineKeyboardButton(
                f"{_DEST_LABEL[dest]}{mark}",
                callback_data=f"dest:{feature}:{dest}"))
        rows.append([InlineKeyboardButton(f"— {label} —",
                                          callback_data="dest:noop")])
        rows.append(row)
    lines += ["",
              "🎬 **Send URL uploads as**",
              "",
              "How video files from links arrive. Photos, audio and "
              "split parts are unaffected, and restricted downloads "
              "always send videos as video — this is URL uploads only.",
              ""]
    current = settings.get_send_as("uploader", user_id)
    lines.append(f"🔗 URL uploads: **{_SENDAS_LABEL[current]}**")
    row = []
    for mode in ("video", "document"):
        mark = " ✓" if mode == current else ""
        row.append(InlineKeyboardButton(
            f"{_SENDAS_LABEL[mode]}{mark}",
            callback_data=f"sendas:uploader:{mode}"))
    rows.append([InlineKeyboardButton("— 🎬 Send URL uploads as —",
                                      callback_data="sendas:noop")])
    rows.append(row)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@Client.on_message(filters.private & admin_only
                   & filters.command("destination"))
async def destination_cmd(_client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    text, markup = _text_and_markup(user_id)
    await message.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^dest:"))
async def destination_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else None
    if not is_admin_id(user_id):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) == 3 and parts[1] in ("uploader", "restricted"):
        settings.set_dest(parts[1], parts[2], user_id)
        # Vault backup right away: the file is the durable copy on
        # HF (Mongo is unreachable), so a redeploy before the next
        # shutdown must not lose the change.
        await backup.backup_kind(client, "data/settings.json",
                                 settings.DATA_FILE)
        text, markup = _text_and_markup(user_id)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        await query.answer("Destination updated ✓")
    else:
        await query.answer()


@Client.on_callback_query(filters.regex(r"^sendas:"))
async def sendas_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else None
    if not is_admin_id(user_id):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if (len(parts) == 3 and parts[1] == "uploader"
            and parts[2] in ("video", "document")):
        settings.set_send_as("uploader", parts[2], user_id)
        await backup.backup_kind(client, "data/settings.json",
                                 settings.DATA_FILE)
        text, markup = _text_and_markup(user_id)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        await query.answer("Upload style updated ✓")
    else:
        await query.answer()
