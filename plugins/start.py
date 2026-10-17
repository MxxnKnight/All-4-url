""" /start and /help — a button menu into per-section help.

The menu is the bot's home screen: four feature buttons plus a Usage
button showing bandwidth and job stats. Each button edits the message
to that section's text, with a Back button returning here. Owner-only,
like every handler in this bot.
"""

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import asyncio
import os
import re
import sys
import time

import config
from core import backup, cookies, stats
from core.utils import human_bytes, is_admin_id, is_owner_id, admin_only, owner_only

#: Koyeb's free monthly outbound allowance, in bytes. Outbound here is
#: everything the box sends out: Telegram uploads + file-to-link serves.
FREE_OUTBOUND_BYTES = 100 * 1024 ** 3

HOME_TEXT = """**All-4-URL** — your personal downloader bot.

Send me a link or a file and I'll handle it. Pick a section below for
details on each feature.

This bot only answers its owner and sudo users. Strangers get nothing.

**Sudo users** (owner only): `/addsudo <id or @username>` or reply `/addsudo`,
`/delsudo <id>`, `/sudos`. Permanent sudo users go in the SUDO_USERS env var.

**Cookies**: when a login-walled site (Instagram, Reddit, …) fails, tap
`/setcookies` and pick the site — then send its `cookies.txt`
(exported from your browser while logged in). Each site gets its own
jar, verified before installing; `/cookies` lists them. Jars are backed
up automatically and survive redeploys.
**yt-dlp**: `/updateytdlp` upgrades it on the spot, no redeploy needed."""

SECTION_UPLOADER = """📥 **URL Uploader**

Just send any link in this chat:
• Videos/audio from YouTube, Instagram, TikTok, X, Reddit, … (yt-dlp,
  with quality buttons when several resolutions are offered)
• Instagram photo posts & carousels, Pinterest, Pixiv galleries (gallery-dl)
• Terabox share links (with a file picker when a share has several files)
• Direct file links

I react 👀 when I start on a link and 👍 when your file lands."""

SECTION_FILETOLINK = """🔗 **File-to-Link**

Send or forward any file **right here in this chat** and I'll reply
with a direct-download link and a streaming link (buttons included).

No channel needed: the links read the file back from the message you
sent here — so don't delete that message, or the links break.

Links expire after the configured hours (default 72).
Optional upgrade: set STORAGE_CHANNEL_ID and files are copied into a
private storage channel instead, so links survive deleting the original."""

SECTION_RESTRICTED = """🔒 **Restricted Downloader**

Send a `t.me/…` link to a post you can see but cannot forward or save
(because the chat restricts it) and I'll re-upload an unrestricted copy
here. Albums and text posts work too.

Send many links at once with /batch (one link per line); /cancel stops
a batch after the current item. Two links from the same album only send
it once — duplicates in a batch are skipped.

Range batching: send exactly two links to the *same chat* — the first
and the last message — and the bot walks *everything between them*
(e.g. t.me/channel/1 + t.me/channel/7 grabs 1, 2, 3, 4, 5, 6, 7).
Deleted messages in the range are counted silently, not spammed.
Three or more links (or links to different chats) run as an explicit
list instead. Ranges are capped at 100 messages per batch.

Batch extras: with 2+ links you first get a content-type filter menu
(toggle text / photos / videos / … on or off), then a delay menu —
pick how many seconds to wait between items before the batch starts.

Needs SESSION_STRING set (a user account that is a member of those
chats does the reading — a bot account can't)."""

SECTION_MEDIAINFO = """📊 **MediaInfo**

Full metadata for any file — container, codecs, resolution, bitrates,
audio/subtitle tracks — without downloading the whole file: I probe
only the first few MB (plus the tail for MP4/MOV/MKV) and publish the
complete report as a Telegraph page.

Four ways to ask:
1. Reply to any media message with /mediainfo
2. Send a file with the caption `/mediainfo`
3. `/mediainfo` with a direct file link (or reply to a message holding
   one) — only the first bits of the link are fetched
4. Tap the 📊 MediaInfo button under a file-to-link or uploader result"""

SECTION_TEXTS = {
    "menu:uploader": SECTION_UPLOADER,
    "menu:filetolink": SECTION_FILETOLINK,
    "menu:restricted": SECTION_RESTRICTED,
    "menu:mediainfo": SECTION_MEDIAINFO,
}

HELP = "\n\n".join([HOME_TEXT, SECTION_UPLOADER, SECTION_FILETOLINK,
                    SECTION_RESTRICTED, SECTION_MEDIAINFO])


def _uptime_text(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _usage_text() -> str:
    snap = stats.snapshot()
    month, total = snap["month"], snap["total"]
    outbound = month["uploaded"] + month["served"]
    pct = min(100.0, outbound / FREE_OUTBOUND_BYTES * 100)

    lines = [f"📊 **Usage** — {snap['month_key']}",
             "",
             f"⏱ Uptime: {_uptime_text(snap['uptime'])}",
             "",
             "**This month**",
             f"📥 Downloaded: {human_bytes(month['downloaded'])}",
             f"📤 Uploaded to Telegram: {human_bytes(month['uploaded'])}",
             f"🌐 Served via links: {human_bytes(month['served'])}",
             f"📡 Outbound: {human_bytes(outbound)} "
             f"({pct:.0f}% of the 100 GB free allowance)",
             "",
             "**All time**",
             f"📥 {human_bytes(total['downloaded'])} down · "
             f"📤 {human_bytes(total['uploaded'])} up · "
             f"🌐 {human_bytes(total['served'])} served"]
    jobs = total["jobs"]
    if jobs:
        bits = []
        for name in ("uploader", "restricted", "file-to-link", "mediainfo"):
            entry = jobs.get(name)
            if entry:
                bit = f"{name} {entry['ok']}✓"
                if entry["failed"]:
                    bit += f" ({entry['failed']} failed)"
                bits.append(bit)
        if bits:
            lines += ["", "📦 Jobs: " + " · ".join(bits)]
    return "\n".join(lines)


def _home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 URL Uploader", callback_data="menu:uploader"),
         InlineKeyboardButton("🔗 File-to-Link", callback_data="menu:filetolink")],
        [InlineKeyboardButton("🔒 Restricted", callback_data="menu:restricted"),
         InlineKeyboardButton("📊 MediaInfo", callback_data="menu:mediainfo")],
        [InlineKeyboardButton("📈 Usage", callback_data="menu:usage")],
    ])


def _back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:home")]])


@Client.on_message(filters.private & admin_only
                   & filters.command(["start", "help"]))
async def start_cmd(_client: Client, message: Message):
    await message.reply_text(HOME_TEXT, reply_markup=_home_markup())


async def _resolve_user(client: Client, message: Message) -> tuple:
    """Resolve an /addsudo target: numeric id, @username, or reply.

    Returns (user_id, None), or (None, error_text) when the input is
    missing or the username can't be found.
    """
    parts = (message.text or "").split()
    if len(parts) > 1:
        raw = parts[1].lstrip("@")
        if raw.isdigit():
            return int(raw), None
        try:
            user = await client.get_users(raw)
            return user.id, None
        except Exception:
            return None, (f"Couldn't find @{raw} — check the spelling, or "
                          "use their numeric id from @userinfobot.")
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, None
    return None, ("Usage: /addsudo <user_id or @username> — or reply to "
                  "their message with /addsudo.")


@Client.on_message(filters.private & owner_only
                   & filters.command("addsudo"))
async def addsudo_cmd(client: Client, message: Message):
    """Owner only: /addsudo <id|@username> — or reply to their message."""
    from core import sudo
    target_id, err = await _resolve_user(client, message)
    if err:
        await message.reply_text(err)
        return
    if sudo.is_owner(target_id):
        await message.reply_text("That's the owner already — no need.")
        return
    if sudo.is_admin(target_id):
        await message.reply_text(f"{target_id} is already a sudo user.")
        return
    sudo.add_sudo(target_id)
    backed = await backup.backup_kind(client, "data/sudo.json",
                                      sudo.DATA_FILE)
    persist = (" Backed up — survives redeploys."
               if backed else
               " Note: this lasts until the next redeploy/restart. For a "
               "permanent sudo user, add their id to the SUDO_USERS env "
               "var instead.")
    await message.reply_text(
        f"✅ {target_id} is now a sudo user — full bot access.{persist}")


@Client.on_message(filters.private & owner_only
                   & filters.command("delsudo"))
async def delsudo_cmd(client: Client, message: Message):
    """Owner only: /delsudo <user_id>."""
    from core import sudo
    parts = (message.text or "").split()
    target_id = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
    if not target_id:
        await message.reply_text("Usage: /delsudo <user_id>")
        return
    env_ids, _ = sudo.list_sudo()
    if target_id in env_ids:
        await message.reply_text(
            f"{target_id} comes from the SUDO_USERS env var — remove them "
            "there and redeploy; /delsudo only removes /addsudo users.")
        return
    if sudo.remove_sudo(target_id):
        await backup.backup_kind(client, "data/sudo.json", sudo.DATA_FILE)
        await message.reply_text(f"Removed {target_id} from sudo users.")
    else:
        await message.reply_text(f"{target_id} is not a sudo user.")


@Client.on_message(filters.private & owner_only
                   & filters.command("sudos"))
async def sudos_cmd(_client: Client, message: Message):
    """Owner only: list sudo users."""
    from core import sudo
    env_ids, runtime_ids = sudo.list_sudo()
    lines = [f"👑 Owner: {config.OWNER_ID}"]
    lines.append("🔧 Env SUDO_USERS: " +
                 (", ".join(str(i) for i in sorted(env_ids)) or "—"))
    lines.append("⏳ Added via /addsudo: " +
                 (", ".join(str(i) for i in sorted(runtime_ids)) or "—"))
    await message.reply_text("\n".join(lines))


def _cookie_menu() -> InlineKeyboardMarkup:
    rows, row = [], []
    for pid, label, _domains in cookies.COOKIE_SITES:
        mark = "🟢 " if os.path.isfile(cookies.jar_path(pid)) else ""
        row.append(InlineKeyboardButton(f"{mark}{label}",
                                        callback_data=f"cookie:{pid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔍 Auto-detect from the file",
                                      callback_data="cookie:auto")])
    rows.append([InlineKeyboardButton("❌ Cancel",
                                      callback_data="cookie:cancel")])
    return InlineKeyboardMarkup(rows)


@Client.on_message(filters.private & owner_only
                   & filters.command("setcookies"))
async def setcookies_cmd(client: Client, message: Message):
    """Owner only: /setcookies -> platform menu; reply-to-file still works."""
    target = message.reply_to_message
    doc = getattr(target, "document", None) if target else None
    if doc:
        # fast path: reply to a cookies.txt -> detect its platform
        cookies.CONSUMED.add(target.id)
        await _install_cookie_file(client, message, target, platform="auto")
        return
    await message.reply_text(
        "Which site are these cookies for?",
        reply_markup=_cookie_menu())


@Client.on_callback_query(filters.regex(r"^cookie:"))
async def cookie_callback(client: Client, query: CallbackQuery):
    if not is_owner_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    action = (query.data or "").split(":", 1)[1]
    owner = query.from_user.id
    if action == "cancel":
        cookies.clear_pending(owner)
        await query.answer("Cancelled.")
        await query.message.edit_text("Cookie install cancelled.")
        return
    if action == "auto" or action in cookies.site_ids():
        cookies.set_pending(owner, action)
        label = ("auto-detect" if action == "auto"
                 else cookies.site_label(action))
        await query.answer()
        await query.message.edit_text(
            f"Send the cookies.txt for **{label}** now (as a file).\n"
            "I'll check it really holds that site's cookies before "
            "installing. You have 10 minutes — or tap ❌ Cancel.")
        return
    await query.answer("Unknown choice.")


@Client.on_message(filters.private & owner_only & filters.document,
                   group=-1)
async def cookie_document(client: Client, message: Message):
    """Owner only: eats cookie files before file-to-link sees them.

    Fires when the owner tapped a /setcookies menu button (pending
    platform) or captioned the file /setcookies. Consumed messages are
    marked so file-to-link skips them.
    """
    owner = message.from_user.id if message.from_user else None
    platform = cookies.take_pending(owner) if owner else None
    caption_hit = bool(re.search(r"(?i)/setcookies",
                                 message.caption or ""))
    if platform is None and not caption_hit:
        return  # not ours — file-to-link handles it normally
    cookies.CONSUMED.add(message.id)
    await _install_cookie_file(client, message, message,
                               platform=platform or "auto")


async def _install_cookie_file(client: Client, message: Message,
                               doc_msg: Message, platform: str) -> None:
    """Validate a cookies.txt and install it as platform's jar.

    platform="auto" detects the site from the file's domains. A chosen
    platform is verified: the file must actually contain that site's
    cookies, otherwise it is rejected with guidance.
    """
    status = await message.reply_text("Checking the cookies file…")
    try:
        tmp = await client.download_media(doc_msg)
    except Exception as exc:
        await status.edit_text(f"Couldn't download that file: {exc}")
        return
    try:
        with open(tmp, encoding="utf-8", errors="replace") as handle:
            text = handle.read(2_000_000)
    except Exception as exc:
        await status.edit_text(f"Couldn't read that file: {exc}")
        return
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    count, domains = cookies.parse_cookies(text)
    if count == 0:
        await status.edit_text(
            "That doesn't look like a Netscape cookies.txt — no cookie "
            "lines found. Export it with a \"Get cookies.txt\" style "
            "extension (while logged into the site) and try again.")
        return
    label = None
    if platform == "auto":
        platform = cookies.detect_platform(domains)
        if platform is None:
            platform = cookies.fallback_platform(domains)
        label = cookies.site_label(platform)
    else:
        label = cookies.site_label(platform)
        _pid, _plabel, site_domains = next(
            s for s in cookies.COOKIE_SITES if s[0] == platform)
        if not any(cookies.domain_matches(d, site_domains)
                   for d in domains):
            found = ", ".join(domains[:4]) or "none"
            await status.edit_text(
                f"⚠️ That file has no {label} cookies in it "
                f"(found: {found}).\n"
                "Export cookies while logged into the right site and try "
                "again — tap /setcookies to restart.")
            return
    existed = os.path.isfile(cookies.jar_path(platform))
    try:
        cookies.install_jar(platform, text)
    except Exception as exc:
        await status.edit_text(f"Couldn't save the cookies: {exc}")
        return
    verb = "Updated" if existed else "Installed"
    login_warn = ""
    if not cookies.looks_logged_in(platform, text):
        login_warn = (f"\n⚠️ These don't look like *logged-in* {label} "
                      "cookies (no session cookie found) — the site may "
                      "still block you. Export again while logged into the "
                      "site and make sure to include all its cookies.")
    backed = await backup.backup_kind(client, f"cookies/{platform}.txt",
                                      cookies.jar_path(platform))
    persist_note = ("\n💾 Backed up — survives redeploys."
                    if backed else
                    "\n⚠️ Backup failed — re-send this file after a "
                    "redeploy. Cookies expire anyway, so re-send a fresh "
                    "file when the site starts failing again.")
    await status.edit_text(
        f"✅ {verb} **{label}** cookies: {count} cookies."
        f"{login_warn}\n"
        "They'll be used from the next download on that site."
        f"{persist_note}")


@Client.on_message(filters.private & owner_only
                   & filters.command("cookies"))
async def cookies_cmd(_client: Client, message: Message):
    """Owner only: list installed cookie jars."""
    jars = cookies.list_jars()
    if not jars:
        await message.reply_text(
            "No cookie jars installed. Use /setcookies to add one.")
        return
    lines = []
    for _pid, label, count, ts in jars:
        when = time.strftime("%Y-%m-%d", time.localtime(ts))
        lines.append(f"• {label}: {count} cookies (installed {when})")
    legacy = config.cookies_path()
    if legacy and os.path.basename(legacy) == "cookies.txt":
        lines.append("• legacy cookies.txt: also present (fallback)")
    await message.reply_text("🍪 **Cookie jars**\n" + "\n".join(lines))


@Client.on_message(filters.private & owner_only
                   & filters.command("updateytdlp"))
async def updateytdlp_cmd(_client: Client, message: Message):
    """Owner only: pip-upgrade yt-dlp right now, no redeploy needed."""
    import subprocess
    from importlib.metadata import PackageNotFoundError, version
    try:
        old = version("yt-dlp")
    except PackageNotFoundError:
        old = "not installed"
    status = await message.reply_text(
        f"Updating yt-dlp (now {old}) — this takes a bit…")

    def _upgrade():
        return subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-U",
             "yt-dlp[default,curl-cffi]"],
            capture_output=True, text=True, timeout=600)

    try:
        proc = await asyncio.get_running_loop().run_in_executor(None,
                                                                _upgrade)
    except Exception as exc:
        await status.edit_text(f"Update failed: {exc}")
        return
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        await status.edit_text(f"pip failed:\n{tail}")
        return
    # Drop already-imported modules so the next lazy import reads the new
    # code from disk immediately (our engines import yt_dlp per call).
    for mod in [m for m in sys.modules
                if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[mod]
    try:
        new = version("yt-dlp")
    except PackageNotFoundError:
        new = "unknown"
    await status.edit_text(
        f"✅ yt-dlp updated: {old} → {new} (active from the next download).\n"
        "Note: on Render free this lasts until the next redeploy — for a "
        "permanent update the requirements.txt pin needs bumping too.")


@Client.on_callback_query(filters.regex(r"^menu:"))
async def menu_callback(_client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    await query.answer()
    try:
        if query.data == "menu:home":
            await query.message.edit_text(HOME_TEXT,
                                          reply_markup=_home_markup())
        elif query.data == "menu:usage":
            await query.message.edit_text(_usage_text(),
                                          reply_markup=_back_markup())
        else:
            text = SECTION_TEXTS.get(query.data or "")
            if text:
                await query.message.edit_text(text,
                                              reply_markup=_back_markup())
    except Exception:
        # e.g. "message is not modified" when tapping the current section
        pass
