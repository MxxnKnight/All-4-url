""" /upage — private web-upload link (owner only).

The bot replies with a signed upload-page link valid for 1 hour.
Files uploaded there land on the server disk; each finished file gets
its own download link, and sending that link back to the bot uploads
it to Telegram straight from the disk copy (never downloaded again),
then deletes the server copy.
"""

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from core import upage
from core.utils import owner_only


@Client.on_message(filters.private & owner_only
                   & filters.command("upage"))
async def upage_cmd(client: Client, message):
    if not config.BASE_URL:
        await message.reply_text(
            "❌ BASE_URL is not set on the server, so I can't build an "
            "upload link. Set it to the Koyeb domain and redeploy.")
        return
    link = upage.page_url()
    await message.reply_text(
        "📤 **Private upload page** — valid for 1 hour.\n\n"
        f"{link}\n\n"
        "Open it in a browser, drop your files (max 750 MB each), and "
        "each finished file gets its own download link. Send that link "
        "back here and I'll upload it to Telegram straight from the "
        "server disk — no re-download — then delete the server copy.\n\n"
        "The page sets no cookies and logs nothing about you. Files "
        "left unused are deleted when the link expires.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📤 Open upload page", url=link)]]))
