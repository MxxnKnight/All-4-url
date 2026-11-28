""" /upzip — collect links into one zip (v1.51.16).

The owner sends /upzip, then every link he sends is downloaded
through the normal chain and appended to one zip instead of being
sent to him. A status message in the chat shows what was added,
the file count and the total size; a temporary web page shows the
same list. "Send as zip" (on the page or in the chat) sends the
zip and ends the session — the page dies with it. "Discard"
throws the whole collection away.

How the pieces fit:
  * SESSIONS holds the one live session per user. The uploader's
    _deliver() is the choke point: when a session is active for
    the sender, downloaded paths land here (add_files) instead
    of going through the unpack prompt and send_files_routed.
    Picker callbacks (format/gdflix) flow through the same
    _deliver, so a pick made mid-session is collected too. The
    yt-dlp format picker and the gdflix Telegram-link choice are
    skipped while collecting (best quality / download straight
    in) — the Terabox multi-file picker is a real file choice,
    so it still asks, and the pick is collected.
  * The zip is a real file from the start (created empty, then
    appended to with ZIP_STORED — media is already compressed,
    deflate would only burn CPU). The page reads the live zip's
    central directory on every load, so it is never stale.
  * The page link is an HMAC-signed token (same shape as the zip
    browser's, but a 24 h expiry — a collection can run longer
    than an unpack picker's hour). The page itself shows only
    names and sizes; Send/Discard need the owner's Telegram
    signature (validate_init_data).
  * A collection survives nothing: a Space restart wipes the
    in-memory session and the zip with it. That is the accepted
    shape — the same as a pending unpack picker.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
import zipfile

from aiohttp import web
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from core.jobs import JobCancelled
from core.sender import send_files_routed
from core.utils import cleanup_dir, human_bytes, make_job_dir
from core.webapp import validate_init_data
from core.webserver import get_bot

#: The page link lives a day — a collection can run longer than
#: an unpack picker's hour. The session itself has no TTL: it
#: ends on Send, Discard, or a restart.
COLLECT_TTL = 86400
#: Just under the splitter's 20 GB total cap, so a finished
#: collection can always be sent (split into parts if needed).
MAX_COLLECT_TOTAL = 19_000_000_000
#: Free disk that must remain after an append.
_DISK_HEADROOM = 256 * 1024 * 1024

_VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "m4v", "ts", "flv"}
_AUDIO_EXTS = {"mp3", "m4a", "flac", "ogg", "opus", "wav", "aac"}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}

#: user_id -> session dict.
SESSIONS: dict[int, dict] = {}
#: token session id -> session dict (for the web page).
RECORDS: dict[str, dict] = {}

KIND_ICON = {"video": "🎬", "audio": "🎧", "image": "🖼️",
             "other": "📄"}


class CollectError(Exception):
    """Bad token, expired page, or a dead session."""


# ------------------------------------------------------------------ tokens

def _secret() -> bytes:
    return (config.BOT_TOKEN or "all4url").encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(session_id: str) -> str:
    payload = {"j": session_id,
               "exp": int(time.time()) + COLLECT_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(),
                        hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str) -> str:
    """Token -> session id, or raise CollectError."""
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise CollectError("bad link")
    expected = _b64(hmac.new(_secret(), body.encode(),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise CollectError("bad link")
    try:
        payload = json.loads(_unb64(body))
        session_id = str(payload["j"])
    except Exception:
        raise CollectError("bad link")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise CollectError("link expired")
    return session_id


# ------------------------------------------------------------------ session

def active(user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    return SESSIONS.get(user_id)


def _kind(ext: str) -> str:
    ext = ext.lower()
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _IMAGE_EXTS:
        return "image"
    return "other"


def _zip_name(session: dict) -> str:
    return time.strftime("collection-%Y%m%d-%H%M.zip",
                         time.localtime(session["started"]))


def page_url(session: dict) -> str:
    """Full page URL ("" when the server has no public URL — then
    the session runs on the chat status message alone)."""
    if not config.BASE_URL:
        return ""
    return f"{config.BASE_URL}/collect/{make_token(session['id'])}"


def status_text(session: dict) -> str:
    files = session["files"]
    lines = ["📦 **Collecting into a zip**",
             "Send links — each one is downloaded and added here, "
             "nothing is sent to you yet."]
    shown = files[-5:]
    for f in shown:
        lines.append(f"✅ {f['name']} · {f['kind']} · "
                     f"{human_bytes(f['size'])}")
    if len(files) > len(shown):
        lines.append(f"…and {len(files) - len(shown)} more — see the page.")
    lines.append(f"\n📁 {len(files)} file(s) · total "
                 f"{human_bytes(session['total'])}")
    for f in session["failed"][-2:]:
        lines.append(f"❌ {f['name']}: {f['reason']}")
    if session.get("sending"):
        lines.append("\n📦 Sending the zip…")
    else:
        lines.append("\nTap 📦 Send as zip when you're done, or 🗑 "
                     "Discard to throw it away.")
    return "\n".join(lines)


def status_markup(session: dict) -> InlineKeyboardMarkup:
    rows = []
    url = page_url(session)
    if url:
        rows.append([InlineKeyboardButton("🌐 View files", url=url)])
    rows.append([
        InlineKeyboardButton("📦 Send as zip",
                             callback_data="collect:send"),
        InlineKeyboardButton("🗑 Discard",
                             callback_data="collect:discard"),
    ])
    return InlineKeyboardMarkup(rows)


async def refresh_status(client, session: dict) -> None:
    """Re-render the status message (best effort — a deleted
    message must never break a download). A session that already
    ended (sent/discarded) is never re-edited: a download
    finishing at the same moment must not overwrite the final
    "Sent as zip" / "Discarded" text."""
    if SESSIONS.get(session["user_id"]) is not session:
        return
    msg_id = session.get("status_msg_id")
    if not msg_id:
        return
    try:
        await client.edit_message_text(
            session["chat_id"], msg_id, status_text(session),
            reply_markup=None if session.get("sending")
            else status_markup(session))
    except Exception:
        pass


async def start(client, chat_id: int, user_id: int) -> dict:
    """Open a session and post its status message."""
    job_dir = make_job_dir()
    session_id = f"collect:{user_id}:{secrets.token_hex(4)}"
    session = {
        "id": session_id, "user_id": user_id, "chat_id": chat_id,
        "job_dir": job_dir, "started": int(time.time()),
        "files": [], "failed": [], "total": 0,
        "sending": False, "lock": asyncio.Lock(),
        "status_msg_id": None,
    }
    session["zip_path"] = os.path.join(job_dir, _zip_name(session))
    with zipfile.ZipFile(session["zip_path"], "w"):
        pass  # a valid empty zip, appended to as links arrive
    SESSIONS[user_id] = session
    RECORDS[session_id] = session
    try:
        msg = await client.send_message(
            chat_id, status_text(session),
            reply_markup=status_markup(session))
        session["status_msg_id"] = msg.id
    except Exception:
        pass
    return session


async def reshow(client, session: dict) -> None:
    """/upzip again while collecting: the old status message is
    far up the chat, so drop it and post a fresh one (with a
    fresh page link) in its place."""
    old_id = session.get("status_msg_id")
    if old_id:
        try:
            await client.delete_messages(session["chat_id"], [old_id])
        except Exception:
            pass
    try:
        msg = await client.send_message(
            session["chat_id"], status_text(session),
            reply_markup=status_markup(session))
        session["status_msg_id"] = msg.id
    except Exception:
        pass


async def add_files(session: dict, client, paths: list[str]) -> list[dict]:
    """Append downloaded files to the session zip.

    Returns one result per path: {"name", "ok", "reason"}. The
    caller's job_dir cleanup deletes the originals afterwards —
    this only reads them. A file is refused (not added) when the
    zip would pass the send cap, when the disk can't hold the
    copy, or while the zip is already sending — the refusal is
    recorded on the status message so nothing vanishes silently.
    """
    results: list[dict] = []
    async with session["lock"]:
        if session.get("sending"):
            for path in paths:
                name = os.path.basename(path)
                results.append({"name": name, "ok": False,
                                "reason": "the zip is already sending"})
                session["failed"].append(
                    {"name": name, "reason": "the zip is already sending"})
            await refresh_status(client, session)
            return results
        try:
            existing = set()
            with zipfile.ZipFile(session["zip_path"]) as zf:
                existing = set(zf.namelist())
        except Exception:
            existing = set()
        for path in paths:
            name = os.path.basename(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if not size:
                results.append({"name": name, "ok": False,
                                "reason": "empty file"})
                session["failed"].append(
                    {"name": name, "reason": "empty file"})
                continue
            if session["total"] + size > MAX_COLLECT_TOTAL:
                reason = ("too big for one zip "
                          f"(over {human_bytes(MAX_COLLECT_TOTAL)})")
                results.append({"name": name, "ok": False, "reason": reason})
                session["failed"].append({"name": name, "reason": reason})
                continue
            try:
                free = shutil.disk_usage(session["job_dir"]).free
            except OSError:
                free = 0
            if free < size + _DISK_HEADROOM:
                reason = "no disk room for it"
                results.append({"name": name, "ok": False, "reason": reason})
                session["failed"].append({"name": name, "reason": reason})
                continue
            arcname = name
            base, dot, ext = name.rpartition(".")
            n = 2
            while arcname in existing:
                arcname = (f"{base} ({n}).{ext}" if dot
                           else f"{name} ({n})")
                n += 1
            try:
                with zipfile.ZipFile(session["zip_path"], "a",
                                     zipfile.ZIP_STORED) as zf:
                    zf.write(path, arcname)
            except Exception as exc:
                reason = f"could not add it: {exc}"
                results.append({"name": name, "ok": False, "reason": reason})
                session["failed"].append({"name": name, "reason": reason})
                continue
            existing.add(arcname)
            kind = _kind(ext if dot else "")
            session["files"].append(
                {"name": arcname, "size": size, "kind": kind})
            session["total"] += size
            results.append({"name": arcname, "ok": True, "reason": ""})
    await refresh_status(client, session)
    return results


def _end_session(session: dict) -> None:
    SESSIONS.pop(session["user_id"], None)
    RECORDS.pop(session["id"], None)


async def send_zip(client, session: dict) -> str:
    """Send the zip and end the session. Returns "sent" |
    "already" | "cancelled" | "failed: ...".

    The page dies only once the send succeeds — a failed send
    leaves the session (and the page) alive so the owner can
    retry from the chat button.
    """
    async with session["lock"]:
        if session.get("sending"):
            return "already"
        if not session["files"]:
            return "failed: nothing collected yet"
        session["sending"] = True
    await refresh_status(client, session)
    zip_path = session["zip_path"]
    name = os.path.basename(zip_path)
    job = None
    try:
        from core import jobs
        job = await jobs.register(client, session["chat_id"],
                                  f"📦 Sending {name}")
        await send_files_routed(client, session["chat_id"], [zip_path],
                                "uploader", caption=name, job=job,
                                user_id=session["user_id"])
    except JobCancelled:
        async with session["lock"]:
            session["sending"] = False
        await refresh_status(client, session)
        return "cancelled"
    except Exception as exc:
        async with session["lock"]:
            session["sending"] = False
        session["failed"].append({"name": name, "reason": str(exc)[:120]})
        await refresh_status(client, session)
        return f"failed: {exc}"
    finally:
        if job is not None:
            try:
                await job.finish()
            except Exception:
                pass
    _end_session(session)
    try:
        await client.edit_message_text(
            session["chat_id"], session["status_msg_id"],
            f"✅ Sent as zip: {name}\n"
            f"📁 {len(session['files'])} file(s) · total "
            f"{human_bytes(session['total'])}")
    except Exception:
        pass
    cleanup_dir(session["job_dir"])
    return "sent"


async def discard(client, session: dict) -> None:
    """Throw the collection away: page dies, zip deleted, the
    status message says so."""
    _end_session(session)
    try:
        await client.edit_message_text(
            session["chat_id"], session["status_msg_id"],
            "🗑 Collection discarded — nothing was sent.")
    except Exception:
        pass
    cleanup_dir(session["job_dir"])


# ------------------------------------------------------------------ web page

def _record(token: str) -> dict:
    session_id = parse_token(token)
    session = RECORDS.get(session_id)
    if session is None:
        raise CollectError("This collection ended — send /upzip again.")
    if not os.path.isfile(session["zip_path"]):
        raise CollectError("The zip is gone — send /upzip again.")
    return session


def _entries_payload(session: dict) -> dict:
    try:
        with zipfile.ZipFile(session["zip_path"]) as zf:
            infos = zf.infolist()
    except Exception:
        infos = []
    out = []
    for i, info in enumerate(infos):
        base = info.filename.rsplit("/", 1)[-1]
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        out.append({
            "i": i, "name": info.filename, "base": base, "dir": "",
            "size": info.file_size, "size_h": human_bytes(info.file_size),
            "ext": ext, "kind": _kind(ext),
            "split": info.file_size > config.SPLIT_PART_BYTES,
        })
    return {
        "ok": True,
        "archive": os.path.basename(session["zip_path"]),
        "total": session["total"],
        "total_h": human_bytes(session["total"]),
        "count": len(out),
        "sending": bool(session.get("sending")),
        "entries": out,
    }


async def _owner(request: web.Request):
    """(fields, body) for a call signed by the session's owner,
    else (None, error response)."""
    try:
        body = await request.json()
    except Exception:
        return None, None, web.json_response(
            {"ok": False, "error": "bad request"}, status=400)
    fields = validate_init_data(str(body.get("initData", "")),
                                config.BOT_TOKEN)
    if fields is None:
        return None, None, web.json_response(
            {"ok": False, "error": "Telegram signature check failed — "
             "open the page from Telegram and try again."}, status=401)
    return fields, body, None


async def page(request: web.Request) -> web.Response:
    try:
        session = _record(request.match_info["token"])
    except CollectError as exc:
        return web.Response(text=f"📦 {exc}", status=410)
    return web.Response(text=COLLECT_HTML.replace(
        "__TOKEN__", request.match_info["token"]).replace(
        "__NAME__", os.path.basename(session["zip_path"])),
        content_type="text/html")


async def api_entries(request: web.Request) -> web.Response:
    try:
        session = _record(request.match_info["token"])
    except CollectError as exc:
        return web.json_response({"ok": False, "error": str(exc)},
                                 status=410)
    fields, _body, err = await _owner(request)
    if err is not None:
        return err
    if fields.get("user_id") != session["user_id"]:
        return web.json_response(
            {"ok": False, "error": "not your collection"}, status=403)
    return web.json_response(_entries_payload(session))


async def api_send(request: web.Request) -> web.Response:
    try:
        session = _record(request.match_info["token"])
    except CollectError as exc:
        return web.json_response({"ok": False, "error": str(exc)},
                                 status=410)
    fields, _body, err = await _owner(request)
    if err is not None:
        return err
    if fields.get("user_id") != session["user_id"]:
        return web.json_response(
            {"ok": False, "error": "not your collection"}, status=403)
    if session.get("sending"):
        return web.json_response(
            {"ok": False, "error": "already sending — check the chat"},
            status=409)
    if not session["files"]:
        return web.json_response(
            {"ok": False, "error": "nothing collected yet"}, status=409)
    bot = get_bot()
    if bot is None:
        return web.json_response(
            {"ok": False, "error": "bot is not ready — try again"},
            status=503)
    asyncio.create_task(send_zip(bot, session))
    return web.json_response({"ok": True})


async def api_discard(request: web.Request) -> web.Response:
    try:
        session = _record(request.match_info["token"])
    except CollectError as exc:
        return web.json_response({"ok": False, "error": str(exc)},
                                 status=410)
    fields, _body, err = await _owner(request)
    if err is not None:
        return err
    if fields.get("user_id") != session["user_id"]:
        return web.json_response(
            {"ok": False, "error": "not your collection"}, status=403)
    bot = get_bot()
    if bot is None:
        return web.json_response(
            {"ok": False, "error": "bot is not ready — try again"},
            status=503)
    await discard(bot, session)
    return web.json_response({"ok": True})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/collect/{token}", page)
    app.router.add_post("/api/collect/{token}/entries", api_entries)
    app.router.add_post("/api/collect/{token}/send", api_send)
    app.router.add_post("/api/collect/{token}/discard", api_discard)


COLLECT_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<title>📦 __NAME__</title><style>
:root{color-scheme:light dark}
body{margin:0;font-family:system-ui,sans-serif;background:var(--tg-theme-bg-color,#fff);
  color:var(--tg-theme-text-color,#000)}
header{position:sticky;top:0;padding:12px 14px;background:var(--tg-theme-bg-color,#fff);
  border-bottom:1px solid var(--tg-theme-hint-color,#8883)}
h1{font-size:17px;margin:0 0 2px}
#sub{font-size:13px;color:var(--tg-theme-hint-color,#888)}
#list{padding:8px 14px 120px}
.row{display:flex;gap:10px;align-items:center;padding:9px 0;
  border-bottom:1px solid var(--tg-theme-hint-color,#8882)}
.ic{font-size:20px;flex:none}
.nm{flex:1;min-width:0;overflow-wrap:anywhere;font-size:14px}
.sz{flex:none;font-size:12px;color:var(--tg-theme-hint-color,#888)}
#bar{position:fixed;left:0;right:0;bottom:0;padding:10px 14px calc(10px + env(safe-area-inset-bottom));
  background:var(--tg-theme-bg-color,#fff);border-top:1px solid var(--tg-theme-hint-color,#8883);
  display:flex;gap:8px}
button{flex:1;padding:12px;border:0;border-radius:10px;font-size:15px;cursor:pointer}
#send{background:var(--tg-theme-button-color,#2481cc);color:var(--tg-theme-button-text-color,#fff)}
#discard{flex:0 0 auto;background:var(--tg-theme-secondary-bg-color,#eee);
  color:var(--tg-theme-destructive-text-color,#c00)}
#refresh{flex:0 0 auto;background:var(--tg-theme-secondary-bg-color,#eee);
  color:var(--tg-theme-text-color,#000)}
button:disabled{opacity:.5}
#note{padding:10px 14px;font-size:13px;color:var(--tg-theme-hint-color,#888)}
</style></head><body>
<header><h1>📦 __NAME__</h1><div id="sub">Loading…</div></header>
<div id="note">Send links in the chat — each one is added here. Tap Send as zip when you're done.</div>
<div id="list"></div>
<div id="bar">
<button id="refresh">↻</button>
<button id="send">📦 Send as zip</button>
<button id="discard">🗑 Discard</button>
</div>
<script>
const tg = window.Telegram.WebApp; tg.ready(); tg.expand();
const token = "__TOKEN__";
const $ = id => document.getElementById(id);
const KIND_ICON = {video:"🎬", audio:"🎧", image:"🖼️", other:"📄", sub:"💬"};
async function load() {
  const r = await fetch(`/api/collect/${token}/entries`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({initData: tg.initData})});
  const d = await r.json();
  if (!d.ok) { $("sub").textContent = d.error || "This collection ended."; return; }
  $("sub").textContent = `${d.count} file(s) · total ${d.total_h}`
    + (d.sending ? " · sending…" : "");
  $("list").innerHTML = d.entries.map(e =>
    `<div class="row"><span class="ic">${KIND_ICON[e.kind] || "📄"}</span>` +
    `<span class="nm">${e.base}</span><span class="sz">${e.size_h}</span></div>`
  ).join("") || `<div id="note">Nothing collected yet — send a link in the chat.</div>`;
  $("send").disabled = d.sending || d.count === 0;
  $("discard").disabled = d.sending;
}
async function act(path, done) {
  const r = await fetch(`/api/collect/${token}/${path}`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({initData: tg.initData})});
  const d = await r.json();
  if (d.ok) { done(); } else { $("sub").textContent = d.error || "Something went wrong."; }
}
$("refresh").onclick = load;
$("send").onclick = () => act("send", () => {
  $("sub").textContent = "Sending the zip — check the chat.";
  $("send").disabled = true; $("discard").disabled = true;
});
$("discard").onclick = () => act("discard", () => {
  $("sub").textContent = "Discarded — nothing was sent.";
  $("list").innerHTML = "";
  $("send").disabled = true; $("discard").disabled = true;
});
load();
</script></body></html>"""
