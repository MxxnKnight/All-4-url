"""File-to-link: send/forward a file to the bot, get a file menu.

The file no longer gets links dumped on it directly. Instead the
bot replies with a menu (plugins/filemenu.py): Generate links,
MediaInfo, Convert, Screenshots, Sample video, Unzip (zips only),
Close — each button opens its own sub-menu or action, and the
file reference is the message itself, so menus work after a
restart as long as that message still exists.

Default (PM) mode needs no setup beyond BASE_URL: the links simply point
back at the message the owner sent in this chat — the bot re-reads the
file from that very message when a link is opened, so nothing is copied
anywhere. The catch follows naturally: delete that message and the
links die with it.

If STORAGE_CHANNEL_ID is set, the bot instead copies the file into that
private channel first and the menu points at the stored copy, which
survives the original message being deleted.

Either way the links are served by our own web server (core/webserver),
which streams the bytes out of the referenced message with
`stream_media`. Tokens are HMAC-signed and carry their own expiry, so
no database is needed.
"""

from pyrogram import Client, filters
from pyrogram.types import Message

import config
from core import filemenu, stats
from core.cookies import CONSUMED
from core.utils import human_bytes, admin_only, react

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


def _is_image_message(message: Message) -> bool:
    """True for photos and image files — which are NOT file-to-link
    material. Sending an image used to instantly produce download /
    stream links, which is exactly the confusion /phost exists to
    avoid: an image is a picture waiting for a host, not a file
    waiting for a download link. Images stay silent until the owner
    replies /phost to one. Videos, audio, animations, stickers and
    non-image documents are unaffected."""
    if message.photo is not None:
        return True
    doc = message.document
    if doc is not None and (doc.mime_type or "").startswith("image/"):
        return True
    return False


@Client.on_message(filters.private & admin_only & MEDIA_FILTER)
async def file_to_link(client: Client, message: Message):
    if message.id in CONSUMED:
        CONSUMED.discard(message.id)
        return  # eaten by the /setcookies handler (group -1)
    if _is_image_message(message):
        return  # an image, not a file-to-link upload — see /phost
    # No BASE_URL gate here anymore: the menu itself works without
    # it (Convert / Screenshots / Sample / Unzip / MediaInfo need
    # no web address). Only the 🔗 Generate links button requires
    # BASE_URL, and it says so when pressed without one.

    name, size, mime = _file_info(message)
    await react(message, "👀")

    if config.STORAGE_CHANNEL_ID:
        # Optional permanent mode: park a copy in the storage channel
        # and point the menu at that copy.
        status = await message.reply_text("Storing the file…")
        try:
            stored = await message.copy(config.STORAGE_CHANNEL_ID)
        except Exception as exc:
            await react(message, "👎")
            stats.record_job("file-to-link", ok=False)
            await status.edit_text(
                f"Could not copy the file into the storage channel: {exc}\n"
                "Is the bot an admin of STORAGE_CHANNEL_ID?")
            return
        token_chat, token_msg = config.STORAGE_CHANNEL_ID, stored.id
    else:
        # Default PM mode: the menu points at *this* message.
        status = await message.reply_text("Making your file menu…")
        token_chat, token_msg = message.chat.id, message.id

    _n, _s, _m, _k, is_video, is_zip = filemenu.file_info(message)
    await status.edit_text(
        filemenu.menu_text(name, size),
        reply_markup=filemenu.main_menu_markup(
            token_chat, token_msg, is_video, is_zip))
    await react(message, "👍")
    stats.record_job("file-to-link", ok=True)
