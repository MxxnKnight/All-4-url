"""Restricted Telegram downloader.

The owner pastes a t.me link to a post they can *see* (their account is
a member) but cannot forward or save because the chat restricts it.
The user client (core/userclient.py) downloads the media through
MTProto and the bot re-uploads it here, un-restricted.

Supported link shapes:
  https://t.me/<channel>/<msg_id>
  https://t.me/c/<internal_id>/<msg_id>          (private chats)
  https://t.me/<channel>/<topic_id>/<msg_id>     (forum topics)

Plus /batch for many links and /cancel to stop one.
"""

import asyncio
import re

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from core.sender import send_files
from core.userclient import SessionNotConfigured, get_user_client
from core.utils import (JOB_SEMAPHORE, cleanup_dir, make_job_dir,
                        owner_only, react)

LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/"
    r"(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)(?:/(\d+))?")

# Per-owner batch state
_cancel_flags: dict[int, asyncio.Event] = {}


def parse_link(url: str):
    """-> (chat_id_or_username, message_id) or None."""
    match = LINK_RE.search(url or "")
    if not match:
        return None
    internal, username, first, second = match.groups()
    message_id = int(second) if second else int(first)
    if internal:
        return int(f"-100{internal}"), message_id
    return username, message_id


async def _fetch_messages(user: Client, chat_id, message_id: int):
    """The message, or the whole media group it belongs to."""
    message = await user.get_messages(chat_id, message_id)
    if message is None or getattr(message, "empty", False):
        raise ValueError("that message does not exist (or the account "
                         "cannot see that chat)")
    if getattr(message, "media_group_id", None):
        try:
            return await user.get_media_group(chat_id, message_id)
        except Exception:
            return [message]
    return [message]


async def _process_one(bot: Client, user: Client, owner_chat: int,
                       chat_id, message_id: int, seen: set) -> str:
    """Download every item, then re-upload together.

    Photos go back out as one Telegram album (media group) when the
    source was an album, instead of arriving as separate messages.

    `seen` holds media_group_ids and (chat, message_id) keys already
    handled in this batch, so two links into the same album (or the
    same message twice) don't re-send it. Returns "done" or "skipped".
    """
    messages = await _fetch_messages(user, chat_id, message_id)
    first = messages[0]
    group_id = getattr(first, "media_group_id", None)
    keys = {(str(chat_id), m.id) for m in messages}
    if group_id and group_id in seen:
        return "skipped"  # this album was already handled in this batch
    if keys & seen:
        return "skipped"  # exact same message(s) already handled
    if group_id:
        seen.add(group_id)
    seen.update(keys)

    # No progress chatter here by design: downloads and re-uploads run
    # quietly, only the batch status line (one edit per link) moves.
    job_dirs: list[str] = []
    paths: list[str] = []
    caption = ""
    try:
        for msg in messages:
            media = (msg.video or msg.document or msg.photo or msg.audio
                     or msg.voice or msg.animation or msg.sticker)
            if media is None:
                text = msg.text or msg.caption
                if text:
                    await bot.send_message(owner_chat, text)
                continue
            if not caption:
                caption = msg.caption or ""
            job_dir = make_job_dir()
            job_dirs.append(job_dir)
            path = await user.download_media(msg, file_name=job_dir + "/")
            if path:
                paths.append(path)
        if paths:
            await send_files(bot, owner_chat, paths, caption=caption)
        return "done"
    finally:
        for job_dir in job_dirs:
            cleanup_dir(job_dir)


async def _run_links(bot: Client, message: Message, links: list[str]):
    await react(message, "👀")
    owner = message.from_user.id
    cancel = _cancel_flags.setdefault(owner, asyncio.Event())
    cancel.clear()
    try:
        user = await get_user_client()
    except SessionNotConfigured as exc:
        await message.reply_text(str(exc))
        return

    status = await message.reply_text(f"Starting {len(links)} link(s)…")
    done = failed = skipped = 0
    seen: set = set()  # albums / messages already handled in this batch
    async with JOB_SEMAPHORE:
        for i, link in enumerate(links, 1):
            if cancel.is_set():
                await status.edit_text(
                    f"Batch cancelled. {done} done, {failed} failed, "
                    f"{skipped} skipped.")
                return
            parsed = parse_link(link)
            if not parsed:
                failed += 1
                continue
            await status.edit_text(f"{i}/{len(links)} — fetching…")
            try:
                outcome = await _process_one(bot, user, message.chat.id,
                                             parsed[0], parsed[1], seen)
                if outcome == "skipped":
                    skipped += 1
                else:
                    done += 1
            except FloodWait as exc:
                await asyncio.sleep(int(getattr(exc, "value", 10)) + 1)
                failed += 1
            except Exception as exc:
                failed += 1
                await message.reply_text(f"Failed: {link}\n{exc}")
            if i < len(links):
                await asyncio.sleep(3)  # be gentle: FloodWait insurance
    summary = f"Done. {done} succeeded, {failed} failed."
    if skipped:
        summary += f" {skipped} skipped (already in this batch)."
    await status.edit_text(summary)
    if done and not failed:
        await react(message, "👍")


@Client.on_message(filters.private & owner_only & filters.regex(r"t\.me/"))
async def restricted_handler(client: Client, message: Message):
    links = [m.group(0) for m in LINK_RE.finditer(message.text or "")]
    # The uploader plugin also sees non-command text; ignore plain links
    # that are commands (/batch handles its own message below).
    if (message.text or "").lstrip().startswith("/"):
        return
    if links:
        await _run_links(client, message, links)


@Client.on_message(filters.private & owner_only & filters.command("batch"))
async def batch_cmd(client: Client, message: Message):
    owner = message.from_user.id
    links = [m.group(0) for m in LINK_RE.finditer(message.text or "")]
    if links:
        await _run_links(client, message, links)
    else:
        # The plain link handler below picks up the follow-up message,
        # so a batch is: /batch, then one message with all the links.
        await message.reply_text(
            "Send the t.me links now — one per line, all in one message.")


@Client.on_message(filters.private & owner_only & filters.command("cancel"))
async def cancel_cmd(_client: Client, message: Message):
    flag = _cancel_flags.get(message.from_user.id)
    if flag:
        flag.set()
    await message.reply_text("Cancelling after the current item finishes…")
