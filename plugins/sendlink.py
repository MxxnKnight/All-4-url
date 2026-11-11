""" /sendlink — permanent private link-upload page (owner + sudo).

The bot replies with a link that NEVER expires. Whoever opens it
can paste a video/file/image link, pick a quality, and the server
downloads it and uploads it to the owner's bot PM — nothing ever
downloads to the visitor's phone. The page is unlisted (noindex,
robots.txt) and link-only: there is no password, the link itself
is the key, so treat it like one.

Sudo users get this too, like /upage: the file always lands in
the OWNER's bot PM, no matter who pasted the link.
"""

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from core import sendlink
from core.utils import admin_only


@Client.on_message(filters.private & admin_only
                   & filters.command("sendlink"))
async def sendlink_cmd(client: Client, message):
    if not config.BASE_URL:
        await message.reply_text(
            "❌ BASE_URL is not set on the server, so I can't build a "
            "page link. Set it to the Koyeb domain and redeploy.")
        return
    link = sendlink.page_url(
        message.from_user.id if message.from_user else None)
    await message.reply_text(
        "🔗 **Private link-upload page** — this link never expires.\n\n"
        f"{link}\n\n"
        "Open it in a browser, paste a video, file, or image link, "
        "and hit Fetch — that's the only tap. The job card drops "
        "below and walks through Probe, Check, Download, Upload, "
        "and the file lands in the admin's bot PM. Best quality, "
        "always — no quality picker, no second button. The box "
        "clears itself after each Fetch, so paste the next link "
        "straight away; up to 5 jobs at once, and the history "
        "deletes itself after an hour.\n\n"
        "The paste box is right at the top. Direct links, YouTube "
        "and 1000+ video sites, Terabox, Instagram posts, Reddit "
        "and Imgur pages all work — same download logic as pasting "
        "a link here in chat.\n\n"
        "⚠️ Only people with this link can open the page, and it "
        "is marked so Google never lists it — but there is no "
        "password, so treat the link itself like one. Anyone "
        "holding it can make the server download links and spend "
        "bandwidth, like a sudo user.\n\n"
        "The page sets no cookies and logs nothing about you.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 Open link-upload page",
                                   url=link)]]))
