"""Restricted Telegram downloader.

The owner pastes a t.me link to a post they can *see* (their account is
a member) but cannot forward or save because the chat restricts it.
The user client (core/userclient.py) downloads the media through
MTProto and the bot re-uploads it here, un-restricted.

Supported link shapes:
  https://t.me/<channel>/<msg_id>
  https://t.me/c/<internal_id>/<msg_id>          (private chats)
  https://t.me/<channel>/<topic_id>/<msg_id>     (forum topics)

Single link: processed immediately. Two links to the *same chat* are a
range: the bot walks every message between them (first…last). Three or
more links, or links to different chats, are processed as an explicit
list. Either way the bot first shows a content-type filter menu (tick
on/off), then a delay menu (Destiny-style), and only then starts the
batch. /cancel stops a running batch.
"""

import asyncio
import re
import time

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from core.sender import send_files_routed
from core.userclient import SessionNotConfigured, get_user_client
from core import stats
from core.utils import (JOB_SEMAPHORE, SizeBlocked, check_size,
                        cleanup_dir, is_admin_id, make_job_dir, admin_only,
                        react, react_to)

LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/"
    r"(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)(?:/(\d+))?(?:[-–](\d+))?")

# Per-owner batch state
_cancel_flags: dict[int, asyncio.Event] = {}

# Content types the batch filter menu can toggle.
FILTER_TYPES = (
    ("text", "Text"),
    ("photo", "Photos"),
    ("video", "Videos"),
    ("document", "Documents"),
    ("audio", "Audio"),
    ("voice", "Voice"),
    ("animation", "GIFs"),
)

# Delay choices (seconds) between batch items, Destiny-style.
DELAY_CHOICES = (3, 5, 10, 30, 60)

# Batch menu sessions: batch_id -> session dict. Expire on use.
BATCH_SESSIONS: dict[str, dict] = {}
BATCH_TTL = 15 * 60  # seconds
_batch_counter = 0


def _next_batch_id() -> str:
    global _batch_counter
    _batch_counter += 1
    return f"b{int(time.time()) % 100000}{_batch_counter}"


def parse_link(url: str):
    """-> (chat_id_or_username, message_id) or None."""
    match = LINK_RE.search(url or "")
    if not match:
        return None
    internal, username, first, second, _range_end = match.groups()
    message_id = int(second) if second else int(first)
    if internal:
        return int(f"-100{internal}"), message_id
    return username, message_id


def parse_link_range(url: str):
    """-> (chat, lo, hi) for a Destiny-style range link, else None.

    `t.me/chat/111-115` (hyphen or en-dash) names a whole range in
    ONE link. Only the plain form counts: with a forum-topic segment
    (t.me/chat/111/222) the last number is the message, and a dash
    suffix there is left alone."""
    match = LINK_RE.search(url or "")
    if not match:
        return None
    internal, username, first, second, range_end = match.groups()
    if second or not range_end:
        return None
    lo, hi = sorted((int(first), int(range_end)))
    if lo == hi:
        return None
    if internal:
        return int(f"-100{internal}"), lo, hi
    return username, lo, hi


class MessageMissing(ValueError):
    """A ranged-batch message that doesn't exist (deleted, most likely)."""


def _expand_range(chat, lo: int, hi: int):
    """The link list + label for messages lo…hi in one chat."""
    count = hi - lo + 1
    if count > config.BATCH_RANGE_LIMIT:
        raise ValueError(
            f"That range is {count} messages — the limit is "
            f"{config.BATCH_RANGE_LIMIT} per batch. Send a smaller range.")
    if isinstance(chat, int):
        internal = str(chat)[4:] if str(chat).startswith("-100") else str(chat)
        expanded = [f"https://t.me/c/{internal}/{i}"
                    for i in range(lo, hi + 1)]
    else:
        expanded = [f"https://t.me/{chat}/{i}" for i in range(lo, hi + 1)]
    return expanded, f"messages {lo}–{hi}", True


def _expand_links(links: list[str]) -> tuple[list[str], str | None, bool]:
    """Expand a range batch into the full message range.

    Two spellings, same result: TWO links to the same chat
    (t.me/channel/1 + t.me/channel/7) or ONE Destiny-style range
    link (t.me/channel/1-7) both become the links for messages
    1…7, so the batch walks everything between (and including)
    them. Returns (links, range_label, is_range). Anything else —
    a plain single link, 3+ links, different chats — comes back
    unchanged.

    Raises ValueError when the range is bigger than BATCH_RANGE_LIMIT.
    """
    if len(links) == 1:
        ranged = parse_link_range(links[0])
        if ranged:
            return _expand_range(*ranged)
        return links, None, False
    if len(links) != 2:
        return links, None, False
    first, second = parse_link(links[0]), parse_link(links[1])
    if not first or not second or str(first[0]) != str(second[0]):
        return links, None, False
    lo, hi = sorted((first[1], second[1]))
    if lo == hi:
        return links, None, False
    return _expand_range(first[0], lo, hi)


def _media_kind(msg) -> str:
    """Classify a fetched message into a FILTER_TYPES key.

    Anything not covered ("other", e.g. stickers) is always kept.
    """
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "animation", None):
        return "animation"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "document", None):
        return "document"
    media = (getattr(msg, "video", None) or getattr(msg, "photo", None)
             or getattr(msg, "animation", None) or getattr(msg, "audio", None)
             or getattr(msg, "voice", None) or getattr(msg, "document", None)
             or getattr(msg, "sticker", None))
    if media is None and (getattr(msg, "text", None)
                          or getattr(msg, "caption", None)):
        return "text"
    return "other"


async def _fetch_messages(user: Client, chat_id, message_id: int):
    """The message, or the whole media group it belongs to."""
    message = await user.get_messages(chat_id, message_id)
    if message is None or getattr(message, "empty", False):
        raise MessageMissing("that message does not exist (or the account "
                             "cannot see that chat)")
    if getattr(message, "media_group_id", None):
        try:
            return await user.get_media_group(chat_id, message_id)
        except Exception:
            return [message]
    return [message]


async def _process_one(bot: Client, user: Client, owner_chat: int,
                       chat_id, message_id: int, seen: set,
                       filters: dict | None = None) -> str:
    """Download every item, then re-upload together.

    Photos go back out as one Telegram album (media group) when the
    source was an album, instead of arriving as separate messages.

    `seen` holds media_group_ids and (chat, message_id) keys already
    handled in this batch, so two links into the same album (or the
    same message twice) don't re-send it. `filters` (from the batch
    filter menu) drops content types the owner toggled off.

    Returns "done" | "skipped" | "filtered".
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

    filters = filters or {}
    # No progress chatter here by design: downloads and re-uploads run
    # quietly, only the batch status line (one edit per link) moves.
    job_dirs: list[str] = []
    paths: list[str] = []
    caption = ""
    handled_any = False
    blocked_msg = ""
    try:
        for msg in messages:
            kind = _media_kind(msg)
            if not filters.get(kind, True):
                continue  # toggled off in the batch filter menu
            media = (msg.video or msg.document or msg.photo or msg.audio
                     or msg.voice or msg.animation or msg.sticker)
            if media is None:
                text = msg.text or msg.caption
                if text:
                    await bot.send_message(owner_chat, text)
                    handled_any = True
                continue
            try:
                check_size(getattr(media, "file_size", 0) or 0,
                           getattr(media, "file_name", None) or "file")
            except SizeBlocked as exc:
                if not blocked_msg:
                    blocked_msg = str(exc)
                continue  # too big for this host — skip the item
            handled_any = True
            if not caption:
                caption = msg.caption or ""
            job_dir = make_job_dir()
            job_dirs.append(job_dir)
            path = await user.download_media(msg, file_name=job_dir + "/")
            if path:
                paths.append(path)
                stats.record_paths("downloaded", [path])
        if paths:
            await send_files_routed(bot, owner_chat, paths, "restricted", caption=caption)
        if handled_any:
            return "done"
        if blocked_msg:
            raise SizeBlocked(blocked_msg)
        return "filtered"
    finally:
        for job_dir in job_dirs:
            cleanup_dir(job_dir)


async def _run_links(bot: Client, message: Message, links: list[str],
                     filters: dict | None = None, delay: int = 3,
                     status_msg: Message | None = None,
                     quiet_missing: bool = False):
    """Run a batch of links.

    `message` is the owner's links message (reactions + failure replies
    go here). `status_msg`, when given, is an existing message (the
    batch menu) that becomes the progress display. `quiet_missing`
    (range batches) counts deleted messages silently instead of
    spamming one failure reply per missing message.
    """
    if stats.bandwidth_blocked():
        await message.reply_text(stats.bandwidth_pause_text())
        return
    await react(message, "👀")
    owner = message.from_user.id
    cancel = _cancel_flags.setdefault(owner, asyncio.Event())
    cancel.clear()
    try:
        user = await get_user_client()
    except SessionNotConfigured as exc:
        await message.reply_text(str(exc))
        return

    if status_msg is None:
        status = await message.reply_text(f"Starting {len(links)} link(s)…")
    else:
        status = status_msg
        await status.edit_text(f"Starting {len(links)} link(s)…")
    done = failed = skipped = filtered = missing = 0
    seen: set = set()  # albums / messages already handled in this batch
    async with JOB_SEMAPHORE:
        for i, link in enumerate(links, 1):
            if cancel.is_set():
                await status.edit_text(
                    f"Batch cancelled. {done} done, {failed} failed, "
                    f"{skipped} skipped, {filtered} filtered out, "
                    f"{missing} missing.")
                return
            parsed = parse_link(link)
            if not parsed:
                failed += 1
                continue
            await status.edit_text(f"{i}/{len(links)} — fetching…")
            try:
                outcome = await _process_one(bot, user, message.chat.id,
                                             parsed[0], parsed[1], seen,
                                             filters)
                if outcome == "skipped":
                    skipped += 1
                elif outcome == "filtered":
                    filtered += 1
                else:
                    done += 1
            except FloodWait as exc:
                await asyncio.sleep(int(getattr(exc, "value", 10)) + 1)
                failed += 1
            except MessageMissing as exc:
                if quiet_missing:
                    missing += 1  # deleted message inside a range: normal
                else:
                    failed += 1
                    await message.reply_text(f"Failed: {link}\n{exc}")
            except Exception as exc:
                failed += 1
                await message.reply_text(f"Failed: {link}\n{exc}")
            if i < len(links):
                await asyncio.sleep(delay)  # chosen in the delay menu
    summary = f"Done. {done} succeeded, {failed} failed."
    if skipped:
        summary += f" {skipped} skipped (already in this batch)."
    if filtered:
        summary += f" {filtered} filtered out."
    if missing:
        summary += f" {missing} missing (deleted)."
    await status.edit_text(summary)
    stats.record_job("restricted", ok=done > 0 and failed == 0)
    if done and not failed:
        await react(message, "👍")


# ------------------------------------------------------------------ batch menus

def _filter_menu(batch: dict) -> InlineKeyboardMarkup:
    """Content-type toggles, all on by default."""
    chosen = batch["filters"]
    buttons, row = [], []
    for key, label in FILTER_TYPES:
        mark = "✅" if chosen[key] else "❌"
        row.append(InlineKeyboardButton(
            f"{mark} {label}", callback_data=f"bf:{batch['id']}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(
        "Next →", callback_data=f"bf:{batch['id']}:next")])
    buttons.append([InlineKeyboardButton(
        "✖ Cancel", callback_data=f"bx:{batch['id']}")])
    return InlineKeyboardMarkup(buttons)


def _delay_menu(batch: dict) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for secs in DELAY_CHOICES:
        row.append(InlineKeyboardButton(
            f"{secs}s", callback_data=f"bd:{batch['id']}:{secs}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(
        "← Back", callback_data=f"bf:{batch['id']}:back")])
    buttons.append([InlineKeyboardButton(
        "✖ Cancel", callback_data=f"bx:{batch['id']}")])
    return InlineKeyboardMarkup(buttons)


def _get_batch(query: CallbackQuery):
    """The live session for a menu callback, or None when stale."""
    parts = (query.data or "").split(":")
    if len(parts) < 2:
        return None, parts
    batch = BATCH_SESSIONS.get(parts[1])
    if batch is None or time.time() - batch["created"] > BATCH_TTL:
        BATCH_SESSIONS.pop(parts[1], None)
        return None, parts
    return batch, parts


async def _start_batch_menu(client: Client, message: Message, links: list[str]):
    """Two or more links: filter menu first, delay menu second.

    Exactly two links to the same chat expand into the full message
    range between them (t.me/channel/1 + t.me/channel/7 walks 1…7).
    """
    try:
        links, range_label, is_range = _expand_links(links)
    except ValueError as exc:
        await react(message, "👎")
        await message.reply_text(str(exc))
        return
    bid = _next_batch_id()
    if range_label:
        menu_text = (f"Batch: {range_label} ({len(links)} items) — "
                     f"choose what to keep:")
    else:
        menu_text = (f"Batch of {len(links)} link(s) — choose what to keep:")
    BATCH_SESSIONS[bid] = {
        "id": bid,
        "links": links,
        "menu_text": menu_text,
        "range_mode": is_range,
        "origin_chat": message.chat.id,
        "origin_msg": message.id,
        "filters": {key: True for key, _ in FILTER_TYPES},
        "created": time.time(),
    }
    await react(message, "👀")
    await message.reply_text(menu_text,
                             reply_markup=_filter_menu(BATCH_SESSIONS[bid]))


@Client.on_callback_query(filters.regex(r"^(bf|bd|bx):"))
async def batch_menu_callback(client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    batch, parts = _get_batch(query)
    if batch is None:
        await query.answer("Session expired — send the links again.",
                           show_alert=True)
        return
    kind = parts[0]

    if kind == "bx":
        BATCH_SESSIONS.pop(batch["id"], None)
        await query.answer("Cancelled.")
        await query.message.edit_text("Batch cancelled.")
        return

    if kind == "bf":
        action = parts[2] if len(parts) > 2 else ""
        if action == "next":
            if not any(batch["filters"].values()):
                await query.answer("Turn on at least one type.",
                                   show_alert=True)
                return
            await query.answer()
            await query.message.edit_text(
                "How long to wait between items?",
                reply_markup=_delay_menu(batch))
            return
        if action == "back":
            await query.answer()
            await query.message.edit_text(
                batch["menu_text"],
                reply_markup=_filter_menu(batch))
            return
        # toggle a content type
        if action in batch["filters"]:
            batch["filters"][action] = not batch["filters"][action]
            await query.message.edit_reply_markup(
                reply_markup=_filter_menu(batch))
        await query.answer()
        return

    if kind == "bd":
        try:
            delay = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("Pick a delay.", show_alert=True)
            return
        BATCH_SESSIONS.pop(batch["id"], None)
        await query.answer("Starting…")
        try:
            user_msg = await client.get_messages(batch["origin_chat"],
                                                 batch["origin_msg"])
        except Exception:
            user_msg = query.message
        await _run_links(client, user_msg, batch["links"],
                         filters=batch["filters"], delay=delay,
                         status_msg=query.message,
                         quiet_missing=batch["range_mode"])


@Client.on_message(filters.private & admin_only & filters.regex(r"t\.me/"))
async def restricted_handler(client: Client, message: Message):
    links = [m.group(0) for m in LINK_RE.finditer(message.text or "")]
    # The uploader plugin also sees non-command text; ignore plain links
    # that are commands (/batch handles its own message below).
    if (message.text or "").lstrip().startswith("/"):
        return
    if not links:
        return
    if len(links) == 1 and not parse_link_range(links[0]):
        await _run_links(client, message, links)
    else:
        await _start_batch_menu(client, message, links)


@Client.on_message(filters.private & admin_only & filters.command("batch"))
async def batch_cmd(client: Client, message: Message):
    owner = message.from_user.id
    links = [m.group(0) for m in LINK_RE.finditer(message.text or "")]
    if links:
        if len(links) == 1 and not parse_link_range(links[0]):
            await _run_links(client, message, links)
        else:
            await _start_batch_menu(client, message, links)
    else:
        # The plain link handler below picks up the follow-up message,
        # so a batch is: /batch, then one message with all the links.
        await message.reply_text(
            "Send the t.me links now — one per line, all in one message.")


@Client.on_message(filters.private & admin_only & filters.command("cancel"))
async def cancel_cmd(_client: Client, message: Message):
    flag = _cancel_flags.get(message.from_user.id)
    if flag:
        flag.set()
    await message.reply_text("Cancelling after the current item finishes…")
