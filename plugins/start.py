""" /start and /help — a button menu into per-section help.

The menu is the bot's home screen: four inline buttons, each editing
the message to that feature's help text, with a Back button returning
here. Owner-only, like every handler in this bot.
"""

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

from core.utils import is_owner_id, owner_only

HOME_TEXT = """**All-4-URL** — your personal downloader bot.

Send me a link or a file and I'll handle it. Pick a section below for
details on each feature.

This bot only answers its owner. Nothing is public."""

SECTION_UPLOADER = """📥 **URL Uploader**

Just send any link in this chat:
• Videos/audio from YouTube, Instagram, TikTok, X, Reddit, … (yt-dlp,
  with quality buttons when several resolutions are offered)
• Instagram photo posts & carousels, Pinterest, Pixiv galleries (gallery-dl)
• Terabox share links (with a file picker when a share has several files)
• Direct file links

I react 👀 when I start on a link and 👍 when your file lands."""

SECTION_FILETOLINK = """🔗 **File-to-Link**

Send or forward any file **right here in this chat** and I'll reply
with a direct-download link and a streaming link (buttons included).

No channel needed: the links read the file back from the message you
sent here — so don't delete that message, or the links break.

Links expire after the configured hours (default 72).
Optional upgrade: set STORAGE_CHANNEL_ID and files are copied into a
private storage channel instead, so links survive deleting the original."""

SECTION_RESTRICTED = """🔒 **Restricted Downloader**

Send a `t.me/…` link to a post you can see but cannot forward or save
(because the chat restricts it) and I'll re-upload an unrestricted copy
here. Albums and text posts work too.

Send many links at once with /batch (one link per line); /cancel stops
a batch after the current item.

Needs SESSION_STRING set (a user account that is a member of those
chats does the reading — a bot account can't)."""

SECTION_MEDIAINFO = """📊 **MediaInfo**

Full metadata for any file — container, codecs, resolution, bitrates,
audio/subtitle tracks — without downloading the whole file: I probe
only the first few MB (plus the tail for MP4/MOV/MKV) and publish the
complete report as a Telegraph page.

Three ways to ask:
1. Reply to any media message with /mediainfo
2. Send a file with the caption `/mediainfo`
3. Tap the 📊 MediaInfo button under a file-to-link or uploader result"""

SECTION_TEXTS = {
    "menu:uploader": SECTION_UPLOADER,
    "menu:filetolink": SECTION_FILETOLINK,
    "menu:restricted": SECTION_RESTRICTED,
    "menu:mediainfo": SECTION_MEDIAINFO,
}

HELP = "\n\n".join([HOME_TEXT, SECTION_UPLOADER, SECTION_FILETOLINK,
                    SECTION_RESTRICTED, SECTION_MEDIAINFO])


def _home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 URL Uploader", callback_data="menu:uploader"),
         InlineKeyboardButton("🔗 File-to-Link", callback_data="menu:filetolink")],
        [InlineKeyboardButton("🔒 Restricted", callback_data="menu:restricted"),
         InlineKeyboardButton("📊 MediaInfo", callback_data="menu:mediainfo")],
    ])


def _back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:home")]])


@Client.on_message(filters.private & owner_only
                   & filters.command(["start", "help"]))
async def start_cmd(_client: Client, message: Message):
    await message.reply_text(HOME_TEXT, reply_markup=_home_markup())


@Client.on_callback_query(filters.regex(r"^menu:"))
async def menu_callback(_client: Client, query: CallbackQuery):
    if not is_owner_id(query.from_user.id if query.from_user else None):
        await query.answer("Not for you.", show_alert=True)
        return
    await query.answer()
    try:
        if query.data == "menu:home":
            await query.message.edit_text(HOME_TEXT,
                                          reply_markup=_home_markup())
        else:
            text = SECTION_TEXTS.get(query.data or "")
            if text:
                await query.message.edit_text(text,
                                              reply_markup=_back_markup())
    except Exception:
        # e.g. "message is not modified" when tapping the current section
        pass
