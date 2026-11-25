""" /live [-t minutes] [-s minutes] <link> — record a live stream
into Telegram, part by part.

    /live -t 10 -s 30 <link to a live stream>

The bot checks the link is live right now (yt-dlp), then ffmpeg
records it — stream-copy, no re-encode — into MP4 parts
(15 minutes each by default, -t changes it). Each finished part
is sent here as a video the moment it closes, then deleted from
the server disk. A ⏹ Stop button on the status message or under
the latest part ends the recording (the part in progress is
finished and sent too); -s stops it by itself after that many
minutes; a stream ending on its own ends it the same way. One
recording at a time.
"""

import asyncio

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from core import live, stats
from core.utils import admin_only, is_admin_id, react

USAGE = (
    "Usage: `/live [-t minutes] [-s minutes] <link>`\n\n"
    "Records a live stream and sends it here in video parts, as "
    "each part finishes — nothing stays on the server disk. "
    "Tap ⏹ Stop under the status message or under the latest "
    "part — or send /cancel — to end the recording.\n\n"
    "• `-t 30` — each part is 30 minutes (5 to 60; default 15)\n"
    "• `-s 20` — stop the whole recording after 20 minutes\n"
    "• Example: `/live -t 10 -s 30 <link>` — 10-minute parts, "
    "stops after 30 minutes\n\n"
    "• Works on live streams yt-dlp can read (YouTube Live, "
    "Twitch, Kick, Stripchat, …)\n"
    "• One recording at a time\n"
    "• The monthly bandwidth cap still applies — the recording "
    "stops if it's reached\n"
    "• A server restart ends the recording (nothing is saved)"
)

#: /live part length, in whole minutes (v1.51.8, moon's syntax:
#: -t sets it; -s sets the total stop time).
_PART_MIN, _PART_MAX = 5, 60


def parse_live_args(args: str) -> tuple[str, int | None, int | None]:
    """Split "/live [-t N] [-s N] <link>" into (url, part minutes,
    total minutes). Either flag may be missing (None = default:
    15-minute parts, no total limit), and flags may sit before
    or after the link. Raises ValueError with a plain-words
    message the bot can send back."""
    tokens = (args or "").split()
    url: str | None = None
    part: int | None = None
    total: int | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-t", "-s"):
            if i + 1 >= len(tokens):
                raise ValueError(
                    f"{tok} needs a number after it — like {tok} 30.")
            try:
                value = int(tokens[i + 1])
            except ValueError:
                raise ValueError(
                    f"{tokens[i + 1]!r} is not a number of minutes — "
                    f"like {tok} 30.")
            if tok == "-t":
                if not _PART_MIN <= value <= _PART_MAX:
                    raise ValueError(
                        f"Part time must be {_PART_MIN} to {_PART_MAX} "
                        f"minutes.")
                part = value
            else:
                if value < 1:
                    raise ValueError(
                        "Stop time must be at least 1 minute.")
                total = value
            i += 2
            continue
        if url is None and not tok.startswith("-"):
            url = tok
        # An unknown -flag is ignored, not an error: a stray token
        # in a pasted line shouldn't kill the recording.
        i += 1
    if url is None:
        raise ValueError(
            "Send a link — /live [-t minutes] [-s minutes] <link>.")
    return url, part, total


_BUSY_TEXT = (
    "🔴 Already recording a live stream — one at a time. "
    "Stop it with the ⏹ Stop button under the latest part, or /cancel."
)


@Client.on_message(filters.command("live") & filters.private & admin_only)
async def live_cmd(client: Client, message: Message):
    if stats.bandwidth_blocked():
        await message.reply_text(stats.bandwidth_pause_text())
        return
    words = (message.text or "").split()
    args = " ".join(words[1:])  # drop the "/live" word
    if not args.strip():
        await message.reply_text(USAGE)
        return
    try:
        url, part, total = parse_live_args(args)
    except ValueError as exc:
        await message.reply_text(f"{exc}\n\n{USAGE}")
        return
    if live.busy():
        await message.reply_text(_BUSY_TEXT)
        return
    await react(message, "🔴")
    status = await message.reply_text("🔍 Checking the link…")
    # The real gate: atomic — a second /live landing between the
    # busy() check above and this line still loses here.
    if not await live.acquire_slot():
        await status.edit_text(_BUSY_TEXT)
        return
    # Fire and forget: the recording runs for hours; the job
    # releases the slot and cleans up when it ends.
    asyncio.create_task(live.run_live_job(client, message, url, status,
                                          segment_min=part,
                                          stop_after_min=total))


@Client.on_callback_query(filters.regex(r"^lv:"))
async def live_callback(_client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] != "stop":
        await query.answer("Stale button.")
        return
    job = live.get_job(parts[2])
    if job is None:
        await query.answer("That recording already ended.")
        return
    if job.stop_requested.is_set():
        await query.answer("Already stopping…")
        return
    job.request_stop()
    await query.answer("Stopping — the last part will be sent shortly.")
