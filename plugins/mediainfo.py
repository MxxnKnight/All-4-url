""" /mediainfo — full metadata for a file, from a partial probe only.

Ways to trigger it:
  1. Reply to any media message with /mediainfo
  2. Send a file with the caption `/mediainfo`
  3. `/mediainfo <link>` — or reply to a message containing a link —
     probes a direct file URL (only the first bits are fetched)
  4. Tap the 📊 MediaInfo button under a file-to-link / uploader result
     (callback data `mi:<chat_id>:<message_id>`)

The probe streams just the first bits of the file (see core/mediainfo),
so even a 2 GB video costs only a few MB of traffic. The full report is
published as a Telegraph page; the chat gets a short summary.
"""

import os
import re

from pyrogram import Client, StopPropagation, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

from core import stats, telegraph
from core.mediainfo import (ProbeError, format_summary, probe_message,
                            probe_url, report_sections)

URL_RE = re.compile(r"https?://[^\s]+")
from core.utils import (cleanup_dir, is_owner_id, make_job_dir, owner_only,
                        react)

MEDIA_FILTER = (filters.video | filters.document | filters.audio
                | filters.animation | filters.voice)


async def _caption_is_mediainfo(_, __, message: Message) -> bool:
    caption = (getattr(message, "caption", None) or "").strip()
    return caption == "/mediainfo" or caption.startswith("/mediainfo ")


caption_mediainfo = filters.create(_caption_is_mediainfo, "CaptionMediaInfo")


async def _probe_and_report(client: Client, chat_id: int, probe,
                            status: Message | None = None,
                            react_msg: Message | None = None,
                            probing_text: str = "🔍 Probing — reading just "
                            "the first bits…") -> None:
    """Run `probe` (a no-arg async callable returning MediaInfoResult)
    and deliver summary + Telegraph link.

    `react_msg` is the user's message to emoji-react on (Destiny-style:
    👀 while working is set by the caller, 👍/👎 here on outcome).
    """
    if status is None:
        status = await client.send_message(chat_id, probing_text)
    else:
        try:
            await status.edit_text(probing_text)
        except Exception:
            pass

    async def _fail(text: str) -> None:
        if react_msg is not None:
            await react(react_msg, "👎")
        stats.record_job("mediainfo", ok=False)
        await status.edit_text(text)

    try:
        result = await probe()
    except ProbeError as exc:
        await _fail(f"📊 MediaInfo failed: {exc}")
        return
    except Exception as exc:
        await _fail(f"📊 MediaInfo failed unexpectedly: {exc}")
        return

    summary = format_summary(result)
    try:
        header, sections = report_sections(result.text)
        if sections:
            url = await telegraph.create_mediainfo_page(
                f"MediaInfo — {result.file_name}",
                result.file_name, header, sections)
        else:  # shouldn't happen, but never lose the report
            url = await telegraph.create_page(
                f"MediaInfo — {result.file_name}", result.text)
    except Exception:
        # Telegraph is a nicety, not a dependency: fall back to a file.
        job_dir = make_job_dir()
        path = os.path.join(job_dir, "mediainfo.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(result.text)
            await status.edit_text(
                summary + "\n\nTelegraph is unreachable right now, so the "
                "full report is attached as a file instead.")
            await client.send_document(chat_id, path,
                                       caption="Full MediaInfo report")
        finally:
            cleanup_dir(job_dir)
        stats.record_job("mediainfo", ok=True)
        if react_msg is not None:
            await react(react_msg, "👍")
        return

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Full MediaInfo ↗", url=url)]])
    await status.edit_text(summary, reply_markup=markup)
    stats.record_job("mediainfo", ok=True)
    if react_msg is not None:
        await react(react_msg, "👍")


@Client.on_message(filters.private & owner_only
                   & filters.command("mediainfo"))
def _first_url(text: str | None) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(").,>\"'") if match else None


async def mediainfo_cmd(client: Client, message: Message):
    parts = (message.text or "").split(None, 1)
    target = message.reply_to_message

    # 1) /mediainfo <link> — probe a direct file URL.
    link = _first_url(parts[1] if len(parts) > 1 else "")
    if link:
        await react(message, "👀")
        await _probe_and_report(
            client, message.chat.id, lambda: probe_url(link),
            react_msg=message,
            probing_text="🔍 Probing the link — fetching just the first bits…")
        return

    # 2) Reply to a message holding a link.
    if target is not None:
        link = _first_url(getattr(target, "text", None)
                          or getattr(target, "caption", None))
        if link:
            await react(message, "👀")
            await _probe_and_report(
                client, message.chat.id, lambda: probe_url(link),
                react_msg=message,
                probing_text="🔍 Probing the link — fetching just the first bits…")
            return

    # 3) Reply to a media message — the classic path.
    if target is not None and (target.document or target.video or target.audio
                               or target.animation or target.voice):
        await react(message, "👀")
        await _probe_and_report(
            client, message.chat.id,
            lambda: probe_message(client, target), react_msg=message)
        return

    await message.reply_text(
        "Reply to a media message with /mediainfo and I'll probe it.\n"
        "You can also send a file with the caption /mediainfo, use "
        "/mediainfo with a direct file link, or tap the 📊 MediaInfo "
        "button under a result.")


@Client.on_message(filters.private & owner_only & MEDIA_FILTER
                   & caption_mediainfo, group=-1)
async def mediainfo_caption(client: Client, message: Message):
    try:
        await react(message, "👀")
        await _probe_and_report(client, message.chat.id,
                                lambda: probe_message(client, message),
                                react_msg=message)
    finally:
        # This message was a MediaInfo request, not a file-to-link upload:
        # stop the group-0 filetolink handler from also processing it.
        raise StopPropagation


@Client.on_callback_query(filters.regex(r"^mi:"))
async def mediainfo_callback(client: Client, query: CallbackQuery):
    if not is_owner_id(query.from_user.id if query.from_user else None):
        await query.answer("Not for you.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Stale button.")
        return
    try:
        chat_id, message_id = int(parts[1]), int(parts[2])
    except ValueError:
        await query.answer("Stale button.")
        return
    await query.answer("Probing…")
    try:
        target = await client.get_messages(chat_id, message_id)
    except Exception:
        target = None
    if target is None or getattr(target, "empty", False):
        await query.answer("That message is gone — links die with it.",
                           show_alert=True)
        return
    chat = query.message.chat.id if query.message else chat_id
    await react(target, "👀")
    await _probe_and_report(client, chat,
                            lambda: probe_message(client, target),
                            react_msg=target)
