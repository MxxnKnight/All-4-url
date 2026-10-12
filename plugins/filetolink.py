"""File-to-link: send/forward a file to the bot, get expiring links.

Default (PM) mode needs no setup beyond BASE_URL: the links simply point
back at the message the owner sent in this chat — the bot re-reads the
file from that very message when a link is opened, so nothing is copied
anywhere. The catch follows naturally: delete that message and the
links die with it.

If STORAGE_CHANNEL_ID is set, the bot instead copies the file into that
private channel first and the links point at the stored copy, which
survives the original message being deleted.

Either way the links are served by our own web server (core/webserver),
which streams the bytes out of the referenced message with
`stream_media`. Tokens are HMAC-signed and carry their own expiry, so
no database is needed.
"""

from pyrogram import Client, filters
from pyrogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                            Message)

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
    if not config.BASE_URL:
        await message.reply_text(
            "File-to-link needs one thing: BASE_URL must be this bot's "
            "public web address (e.g. your https://….koyeb.app URL), so "
            "the download/stream links have somewhere to point. Set it "
            "and restart the bot — no channel or other setup is needed.")
        return

    name, size, mime = _file_info(message)

    if config.STORAGE_CHANNEL_ID:
        # Optional permanent mode: park a copy in the storage channel
        # and point the links at that copy.
        status = await message.reply_text("Storing the file…")
        try:
            stored = await message.copy(config.STORAGE_CHANNEL_ID)
        except Exception as exc:
            await status.edit_text(
                f"Could not copy the file into the storage channel: {exc}\n"
                "Is the bot an admin of STORAGE_CHANNEL_ID?")
            return
        token_chat, token_msg = config.STORAGE_CHANNEL_ID, stored.id
        note = "Stored permanently in your storage channel."
    else:
        # Default PM mode: the links read the file from *this* message.
        status = await message.reply_text("Making your links…")
        token_chat, token_msg = message.chat.id, message.id
        note = ("Links read the file from this chat — don't delete that "
                "message or the links break.")

    token = make_token(token_chat, token_msg, name or "file", size, mime)
    download_url = f"{config.BASE_URL}/d/{token}"
    stream_url = f"{config.BASE_URL}/stream/{token}"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download", url=download_url),
         InlineKeyboardButton("▶️ Stream", url=stream_url)],
        [InlineKeyboardButton(
            "📊 MediaInfo",
            callback_data=f"mi:{token_chat}:{token_msg}")],
    ])
    await status.edit_text(
        f"**{name}** ({human_bytes(size)})\n\n"
        f"⬇️ Download: {download_url}\n"
        f"▶️ Stream: {stream_url}\n\n"
        f"{note}\n"
        f"Links expire in {config.LINK_EXPIRY_HOURS} hours.",
        reply_markup=markup)
