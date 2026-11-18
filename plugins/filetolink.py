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

Exception: a .txt file full of links (v1.44.0) never gets a menu —
the bot reads the links (one per line, up to 50) and runs them
through the uploader one by one, exactly as if they had been
pasted in chat.
"""

from pyrogram import Client, filters
from pyrogram.types import Message

import config
from core import filemenu, stats
from core.cookies import CONSUMED
from core.utils import (admin_only, cleanup_dir, human_bytes, make_job_dir,
                        react)

#: A links file may hold at most this many links (vs 10 per chat
#: message — the file exists for bigger batches).
MAX_TXT_LINKS = 50
#: A links file may not be bigger than this (a list of links is tiny).
MAX_TXT_BYTES = 1024 * 1024

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
    stream links, which is exactly the confusion /imghost exists to
    avoid: an image is a picture waiting for a host, not a file
    waiting for a download link. Images stay silent until the owner
    replies /imghost to one. Videos, audio, animations, stickers and
    non-image documents are unaffected."""
    if message.photo is not None:
        return True
    doc = message.document
    if doc is not None and (doc.mime_type or "").startswith("image/"):
        return True
    return False


def _links_from_text(text: str) -> list[str]:
    """One link per line, de-duplicated, in order.

    Same rules as the chat multi-link extractor: http(s) only,
    trailing punctuation stripped, t.me links skipped (the
    restricted plugin owns those).
    """
    from urllib.parse import urlparse

    links: list[str] = []
    for line in (text or "").splitlines():
        url = line.strip().rstrip(").,>\"'")
        if not url.lower().startswith(("http://", "https://")):
            continue
        host = (urlparse(url).netloc or "").lower()
        if host in ("t.me", "telegram.me") or host.endswith(
                (".t.me", ".telegram.me")):
            continue
        if url not in links:
            links.append(url)
    return links


async def _handle_txt_links(client: Client, message: Message,
                            size: int) -> bool:
    """A .txt file of links: download, parse, run them (v1.44.0).

    Always returns True — a .txt file is handled here, never by
    the file menu (even when it holds no links, which gets a
    plain note instead of a menu).
    """
    if size > MAX_TXT_BYTES:
        await message.reply_text(
            "That text file is over 1 MB — a list of links should "
            "be much smaller than that.")
        return True
    job_dir = make_job_dir()
    try:
        path = await client.download_media(message, file_name=job_dir)
        if not path:
            await message.reply_text(
                "❌ Couldn't download that text file back.")
            return True
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError as exc:
            await message.reply_text(f"❌ Couldn't read that file: {exc}")
            return True
        links = _links_from_text(text)
        if not links:
            await message.reply_text(
                "No links in that file — put one link per line and "
                "send it again.")
            return True
        if len(links) > MAX_TXT_LINKS:
            await message.reply_text(
                f"Taking the first {MAX_TXT_LINKS} links — the rest "
                "were skipped. Send them in another file.")
            links = links[:MAX_TXT_LINKS]
        await message.reply_text(
            f"Found {len(links)} link(s) — working through them one "
            "by one…")
        from plugins.uploader import _run_links
        await _run_links(client, message, links)
        return True
    finally:
        cleanup_dir(job_dir)


@Client.on_message(filters.private & admin_only & MEDIA_FILTER)
async def file_to_link(client: Client, message: Message):
    if message.id in CONSUMED:
        CONSUMED.discard(message.id)
        return  # eaten by the /setcookies handler (group -1)
    if _is_image_message(message):
        return  # an image, not a file-to-link upload — see /imghost
    # No BASE_URL gate here anymore: the menu itself works without
    # it (Convert / Screenshots / Sample / Unzip / MediaInfo need
    # no web address). Only the 🔗 Generate links button requires
    # BASE_URL, and it says so when pressed without one.

    name, size, mime = _file_info(message)
    if (message.document is not None
            and ((name or "").lower().endswith(".txt")
                 or (mime or "") == "text/plain")):
        # A text file of links runs the uploader, not the menu.
        await _handle_txt_links(client, message, size)
        return
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

    _n, _s, _m, _k, is_video, is_archive = filemenu.file_info(message)
    await status.edit_text(
        filemenu.menu_text(name, size),
        reply_markup=filemenu.main_menu_markup(
            token_chat, token_msg, is_video, is_archive))
    await react(message, "👍")
    stats.record_job("file-to-link", ok=True)
