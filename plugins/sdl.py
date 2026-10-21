"""/sdl — split download: an oversized direct-link video, in parts.

    /sdl <direct video link>

The bot probes the link first (no downloading) and answers with a
report card — direct link? really a video? range/seek support?
total size, duration, and the part plan under BOTH caps (time and
size). Only when everything checks out does the card carry a
✂️ Start button; the job itself lives in core/segmenter.py
(one at a time, disk-checked per part, bandwidth-guarded).
"""

import re
import time

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from core import segmenter, stats
from core.extractors.direct import check_direct
from core.utils import (ThrottledEditor, admin_only, human_bytes,
                        is_admin_id, react_to, run_blocking)

URL_RE = re.compile(r"https?://[^\s]+")

#: Probed jobs waiting on the Start button: job_id -> job dict.
PENDING: dict[str, dict] = {}
JOB_TTL = 1800  # a probe is stale after 30 minutes
_counter = 0


def _next_job_id() -> str:
    global _counter
    _counter += 1
    return f"s{int(time.time()) % 100000}{_counter}"


def _extract_url(message: Message) -> str | None:
    parts = (message.text or "").split(None, 1)
    texts = []
    if len(parts) > 1:
        texts.append(parts[1])
    if message.reply_to_message and message.reply_to_message.text:
        texts.append(message.reply_to_message.text)
    for text in texts:
        match = URL_RE.search(text)
        if match:
            return match.group(0).rstrip(").,>]\"'")
    return None


def _card_text(info: dict, probed, range_ok: bool,
               problems: list[str]) -> str:
    name = info.get("filename") or "file"
    lines = ["✂️ **Split Download — probe result**",
             f"📄 {name}",
             "🔗 Direct link: ✅ yes"]
    if probed:
        lines.append(f"🎞 Video: ✅ {probed['width']}×"
                     f"{probed['height']}")
    else:
        lines.append("🎞 Video: ❌ couldn't read a video stream")
    lines.append(f"🎯 Range/seek support: "
                 f"{'✅ yes' if range_ok else '❌ no'}")
    if probed:
        total_size = info.get("size") or probed["size"]
        size_txt = human_bytes(total_size) if total_size else "unknown"
        mbps = probed["bitrate"] / 1_000_000
        lines.append(f"📦 Size: {size_txt} · ⏱ "
                     f"{segmenter.fmt_time(probed['duration'])} · "
                     f"~{mbps:.1f} Mbps")
    if problems:
        lines += ["", "❌ " + problems[0]]
        return "\n".join(lines)
    max_seconds, max_bytes = segmenter.caps()
    plan = segmenter.estimate_plan(probed["duration"],
                                   probed["bitrate"],
                                   max_seconds, max_bytes)
    lines.append(f"🧩 Plan: {len(plan)} part(s) "
                 f"(≤ {config.SEGMENT_MAX_MIN} min AND ≤ "
                 f"{human_bytes(max_bytes)} each)")
    for i, part in enumerate(plan[:6], 1):
        est = (f" · ~{human_bytes(part['est_bytes'])}"
               if part["est_bytes"] else "")
        lines.append(f"   Part {i} ≈ "
                     f"{segmenter.fmt_time(part['duration'])}{est}")
    if len(plan) > 6:
        lines.append(f"   … +{len(plan) - 6} more")
    lines.append("\nNothing is downloaded until you tap Start.")
    return "\n".join(lines)


@Client.on_message(filters.private & admin_only
                   & filters.command(["sdl"]))
async def sdl_cmd(_client: Client, message: Message):
    url = _extract_url(message)
    if not url:
        await message.reply_text(
            "Usage: `/sdl <direct video link>` — I'll probe it and "
            "show whether it can be split into watchable parts.")
        return
    status = await message.reply_text("🔍 Probing the link…")
    info = await check_direct(url)
    if not info:
        await status.edit_text(
            "❌ That's not a direct file link — /sdl only works on "
            "links that point straight at a video file. (Page links "
            "go through the normal uploader.)")
        return
    final_url = info.get("final_url") or url
    range_ok = await segmenter.check_range_support(final_url)
    probed = await run_blocking(segmenter.probe_remote, final_url)

    problems: list[str] = []
    total_size = info.get("size") or (probed or {}).get("size") or 0
    if not segmenter.looks_like_video(info):
        problems.append("That file doesn't look like a video — "
                          "splitting is for videos only.")
    elif probed is None:
        problems.append("I couldn't read a video stream from that "
                        "link (the host may be blocking the probe).")
    elif not range_ok:
        problems.append("The host doesn't support range/seek "
                        "requests, so I can't fetch the video in "
                        "time windows — splitting it here isn't "
                        "possible without downloading it whole.")
    elif total_size and total_size <= config.MAX_FILE_SIZE:
        problems.append("Good news instead: this fits under the "
                        "normal 2 GB limit — just send the link "
                        "without /sdl and I'll fetch it as one file.")
    elif total_size > config.SEGMENT_MAX_TOTAL_BYTES:
        problems.append(
            f"That file is {human_bytes(total_size)} — split mode "
            f"tops out at "
            f"{human_bytes(config.SEGMENT_MAX_TOTAL_BYTES)}.")
    text = _card_text(info, probed, range_ok, problems)
    if problems:
        await status.edit_text(text)
        return
    job_id = _next_job_id()
    PENDING[job_id] = {
        "url": final_url, "info": info, "probed": probed,
        "origin_chat": message.chat.id, "origin_msg": message.id,
        "ts": time.time(),
    }
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✂️ Start splitting",
                              callback_data=f"sdl:start:{job_id}")],
        [InlineKeyboardButton("❌ Dismiss",
                              callback_data=f"sdl:cancel:{job_id}")],
    ])
    await status.edit_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^sdl:"))
async def sdl_callback(client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Stale button.")
        return
    action, job_id = parts[1], parts[2]
    job = PENDING.get(job_id)
    if action == "cancel":
        PENDING.pop(job_id, None)
        await query.answer("Dismissed.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return
    if action != "start":
        await query.answer("Stale button.")
        return
    if not job or time.time() - job["ts"] > JOB_TTL:
        PENDING.pop(job_id, None)
        await query.answer("This probe expired — run /sdl again.",
                           show_alert=True)
        return
    if segmenter.busy():
        await query.answer(
            "✂️ A split job is already running — one at a time. "
            "Wait for it to finish.", show_alert=True)
        return
    if stats.bandwidth_blocked():
        await query.answer("⛔ Bandwidth paused", show_alert=True)
        try:
            await query.message.edit_text(stats.bandwidth_pause_text())
        except Exception:
            pass
        return
    PENDING.pop(job_id, None)
    await query.answer("Starting…")
    status = query.message
    editor = ThrottledEditor(status)
    try:
        await status.edit_reply_markup(None)
    except Exception:
        pass
    try:
        result = await segmenter.run_segment_job(
            client, status, job["url"], job["info"], job["probed"],
            status, editor)
        summary = (f"✅ **Split download complete** — "
                   f"{result['parts']} part(s) sent:\n"
                   + "\n".join(result["lines"]))
        await editor.edit(summary, force=True)
        await react_to(client, job["origin_chat"], job["origin_msg"],
                       "👍")
        stats.record_job("sdl", ok=True)
    except segmenter.SegmentError as exc:
        await editor.edit(f"❌ Split download stopped: {exc}",
                          force=True)
        await react_to(client, job["origin_chat"], job["origin_msg"],
                       "👎")
        stats.record_job("sdl", ok=False)
    except Exception as exc:
        await editor.edit(f"❌ Split download failed: {exc}",
                          force=True)
        await react_to(client, job["origin_chat"], job["origin_msg"],
                       "👎")
        stats.record_job("sdl", ok=False)
