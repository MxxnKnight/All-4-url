"""File-to-link: send a file, get expiring download + stream links.

The file is copied into the owner's private storage channel (Telegram
itself is the file host — the free Koyeb disk is ephemeral and tiny).
The links point at our own web server, which streams the bytes back
out of that channel message. Tokens are HMAC-signed and carry their
own expiry, so no database is needed.
"""

from pyrogram import Client, filters
from pyrogram.types import Message

import config
from core.linktoken import make_token
from core.utils import human_bytes, owner_only

MEDIA_FILTER = (filters.video | filters.document | filters.photo
                | filters.audio | filters.voice | filters.animation)


def _file_info(message: Message):
    """(file_name, file_size, mime) for whichever media the message holds."""
    for kind in ("document", "video", "audio", "voice", "animation", "sticker"):
        media = getattr(message, kind, None)
        if media is not None:
            return (getattr(media, "file_name", None) or f"{kind}",
                    getattr(media, "file_size", 0) or 0,
                    getattr(media, "mime_type", "") or "")
    if message.photo is not None:
        return ("photo.jpg", message.photo.file_size or 0, "image/jpeg")
    return (None, 0, "")


@Client.on_message(filters.private & owner_only & MEDIA_FILTER)
async def file_to_link(client: Client, message: Message):
    if not config.STORAGE_CHANNEL_ID:
        await message.reply_text(
            "File-to-link is not configured yet.\n\n"
            "1. Create a private channel (any name, e.g. 'All-4-URL storage')\n"
            "2. Add this bot to it as an **admin**\n"
            "3. Forward any message from the channel to @userinfobot-style "
            "id bots or use a channel-id helper to get its -100… id\n"
            "4. Set STORAGE_CHANNEL_ID to that id and restart the bot\n\n"
            "Full steps are in the README.")
        return
    if not config.BASE_URL:
        await message.reply_text(
            "STORAGE_CHANNEL_ID is set, but BASE_URL is not. BASE_URL must "
            "be this bot's public web address (e.g. your "
            "https://….koyeb.app URL) so the links can point anywhere. "
            "Set it and restart the bot.")
        return

    status = await message.reply_text("Storing the file…")
    try:
        stored = await message.copy(config.STORAGE_CHANNEL_ID)
    except Exception as exc:
        await status.edit_text(
            f"Could not copy the file into the storage channel: {exc}\n"
            "Is the bot an admin of STORAGE_CHANNEL_ID?")
        return

    name, size, mime = _file_info(message)
    token = make_token(config.STORAGE_CHANNEL_ID, stored.id,
                       name or "file", size, mime)
    await status.edit_text(
        f"**{name}** ({human_bytes(size)})\n\n"
        f"⬇️ Download: {config.BASE_URL}/d/{token}\n"
        f"▶️ Stream: {config.BASE_URL}/stream/{token}\n\n"
        f"Links expire in {config.LINK_EXPIRY_HOURS} hours.")
