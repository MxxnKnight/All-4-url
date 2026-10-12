""" /start and /help — owner-only, like every handler in this bot. """

from pyrogram import Client, filters
from pyrogram.types import Message

from core.utils import owner_only

HELP = """**All-4-URL** — your personal downloader bot.

**URL uploader**
Just send any link in this chat:
• Videos/audio from YouTube, Instagram, TikTok, X, Reddit, … (yt-dlp)
• Instagram photo posts & carousels, Pinterest, Pixiv galleries (gallery-dl)
• Terabox share links
• Direct file links

**File-to-link**
Send me any file and I'll store it and give you back an expiring
direct-download link and a streaming link.

**Restricted Telegram posts**
Send a `t.me/…` link of a post you can see but cannot forward/save,
and I'll re-upload it here. Send several at once with /batch
(one link per line), and /cancel to stop a batch.

This bot only answers its owner. Nothing is public."""


@Client.on_message(filters.private & owner_only & filters.command("start"))
async def start_cmd(_client: Client, message: Message):
    await message.reply_text(
        "Hey! Send me a link or a file and I'll handle it.\n\n" + HELP)


@Client.on_message(filters.private & owner_only & filters.command("help"))
async def help_cmd(_client: Client, message: Message):
    await message.reply_text(HELP)
