"""Private web-upload page (the /upage command).

Flow: the owner (or a sudo user) runs /upage, the bot replies with a signed page link
valid for 1 hour. The page uploads files in 16 MB chunks (each chunk is
its own short request, so a slow uplink or a Koyeb edge timeout can't
kill a big upload, and a dropped connection resumes from the last
byte the server actually wrote). When a file finishes, the page shows a signed download link plus
three buttons: Copy link, Upload to admin, Delete. Upload to admin
uploads the file to the owner's bot PM straight from disk (no
Telegram link step at all); Delete removes the disk copy and kills
its link at once, but is refused while an admin upload is in
progress, and the page asks for confirmation if the file has not
been sent to the admin yet. Sending the link to the bot still
works the old way, and also lands in the owner's bot PM no matter
who uploaded. The disk copy is deleted the moment any Telegram
upload succeeds, and a used download link stops working.

Privacy: the page is fully self-contained (no external scripts,
fonts or images), the server sets no cookies, and no route here ever
reads or logs the visitor's IP or user-agent. Nothing about the
uploader is recorded anywhere except the file itself, until it is
deleted.

Limits (the 2 GB free disk is the whole budget):
- one file: 750 MB (user-facing limit), 1 GB absolute hard ceiling
- all files waiting on disk together: 1 GB
- while a big file waits, other bot downloads are restricted: the
  pending bytes are added to the free-space headroom in check_size.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import time
from urllib.parse import quote

from aiohttp import web

import config
from core.extractors.direct import safe_filename

log = logging.getLogger("all4url.upage")

#: Page links and download links both live 1 hour.
PAGE_TTL = 3600
FILE_TTL = 3600
#: Per-file limit the page advertises, and the absolute ceiling.
MAX_FILE_BYTES = 750 * 1024 * 1024
HARD_FILE_BYTES = 1000 * 1024 * 1024
#: All unfinished + unclaimed uploads together may not exceed this.
TOTAL_PENDING_BYTES = 1000 * 1024 * 1024
#: Chunk size the page uploads in (one short request per chunk).
CHUNK_SIZE = 16 * 1024 * 1024
#: Free-space margin a new upload must leave on the disk.
HEADROOM = 256 * 1024 * 1024

UPLOAD_DIR = os.path.join(config.BASE_DIR, "uploads")

#: Bot client, set by webserver.start_web_server() via bind_bot().
#: Needed for the page's "Upload to admin" button, which uploads a
#: finished file to the owner's bot PM straight from disk — no
#: Telegram link step at all. None until the bot is ready.
_bot = None

#: File ids currently being uploaded to the admin from the page.
#: A delete for one of these must wait — deleting mid-upload
#: would pull the file out from under the Telegram upload.
_ADMIN_SENDING: set[str] = set()


def bind_bot(client) -> None:
    global _bot
    _bot = client


class TokenError(Exception):
    """Bad signature, malformed token, or expired link."""


class OffsetMismatch(Exception):
    """Chunk arrived at the wrong offset; client must resume."""

    def __init__(self, received: int):
        super().__init__(f"resume from {received}")
        self.received = received


def _secret(domain: str) -> bytes:
    return (config.BOT_TOKEN or "all4url").encode() + b":" + domain.encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(domain: str, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(domain), body.encode(),
                       hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify(domain: str, token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise TokenError("malformed token")
    expected = _b64(hmac.new(_secret(domain), body.encode(),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise TokenError("bad signature")
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        raise TokenError("malformed payload")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("link expired")
    return payload


def make_page_token(user_id: int | None = None) -> str:
    payload = {"exp": int(time.time()) + PAGE_TTL,
               "r": secrets.token_hex(8)}
    if user_id:
        # So the admin-upload note can name the uploader when known.
        # Old tokens without "uid" still verify — they just read as
        # anonymous, which is how every page token worked before.
        payload["uid"] = int(user_id)
    return _sign("upage-page", payload)


def verify_page_token(token: str) -> dict:
    return _verify("upage-page", token)


def make_download_token(file_id: str, expires: int) -> str:
    return _sign("upage-file", {"id": file_id, "exp": expires})


def verify_download_token(token: str) -> str:
    return _verify("upage-file", token)["id"]


# ---------------------------------------------------------------- state
# In-memory on purpose: these records are only valid while the files
# they point at are on this instance's ephemeral disk. A restart or a
# scale-to-zero wake loses the disk anyway, so stale records would
# only ever point at files that no longer exist.
_SESSIONS: dict[str, dict] = {}  # sid -> {name, size, received, tmp, exp}
_FILES: dict[str, dict] = {}      # fid -> {path, name, size, exp, used}


def pending_bytes() -> int:
    """Bytes currently reserved on disk by unfinished + unclaimed
    uploads. Other bot downloads treat this as extra headroom."""
    now = int(time.time())
    total = sum(s["received"] for s in _SESSIONS.values()
                if s["exp"] > now)
    total += sum(f["size"] for f in _FILES.values()
                 if not f["used"] and f["exp"] > now)
    return total


def prune() -> None:
    """Drop expired sessions/files (and their disk copies). Cheap;
    called on every upage request."""
    now = int(time.time())
    for sid, s in list(_SESSIONS.items()):
        if s["exp"] <= now:
            _drop_session(sid)
    for fid, f in list(_FILES.items()):
        if f["used"] or f["exp"] <= now:
            _drop_file(fid)


def startup_cleanup() -> None:
    """Fresh instance, fresh uploads dir: nothing from a previous
    life can be trusted (records are gone with the old process)."""
    _SESSIONS.clear()
    _FILES.clear()
    try:
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    except Exception:
        pass
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _drop_session(sid: str) -> None:
    s = _SESSIONS.pop(sid, None)
    if s:
        try:
            os.unlink(s["tmp"])
        except OSError:
            pass


def _drop_file(fid: str) -> None:
    f = _FILES.pop(fid, None)
    if f:
        try:
            os.unlink(f["path"])
        except OSError:
            pass


def _free_disk() -> int | None:
    try:
        return shutil.disk_usage(config.BASE_DIR).free
    except Exception:
        return None


def start_session(name: str, size: int, uid: int | None = None) -> dict:
    """Create (or resume) an upload session. Raises ValueError with a
    user-facing message when the file can't be accepted."""
    prune()
    size = int(size or 0)
    if size <= 0:
        raise ValueError("empty file")
    if size > MAX_FILE_BYTES:
        raise ValueError("over the 750 MB upload limit")
    if size > HARD_FILE_BYTES:
        raise ValueError("over the 1 GB hard limit")
    safe = safe_filename(name)
    for sid, s in _SESSIONS.items():
        if s["exp"] > int(time.time()) and s["name"] == safe \
                and s["size"] == size:
            return {"sid": sid, "received": s["received"]}  # resume
    if pending_bytes() + size > TOTAL_PENDING_BYTES:
        raise ValueError(
            "another upload is already waiting on disk — send it to "
            "the bot (or wait for it to expire) before uploading more")
    free = _free_disk()
    if free is not None and size + HEADROOM > free:
        raise ValueError("not enough free disk on the server right now")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    sid = secrets.token_hex(12)
    tmp = os.path.join(UPLOAD_DIR, sid + ".part")
    with open(tmp, "wb"):
        pass
    _SESSIONS[sid] = {"name": safe, "size": size, "received": 0,
                      "tmp": tmp, "exp": int(time.time()) + PAGE_TTL,
                      "uid": uid}
    return {"sid": sid, "received": 0}


def get_session(sid: str) -> dict | None:
    s = _SESSIONS.get(sid)
    if s is None or s["exp"] <= int(time.time()):
        return None
    return s


def find_session(name: str, size: int) -> dict | None:
    """A live session for the same file (page reloaded mid-upload)."""
    safe = safe_filename(name)
    for sid, s in _SESSIONS.items():
        if s["exp"] > int(time.time()) and s["name"] == safe \
                and s["size"] == size:
            return {"sid": sid, "received": s["received"]}
    return None


def abort_session(sid: str) -> None:
    _drop_session(sid)


def append_bytes(sid: str, offset: int, piece: bytes) -> int:
    """Write one streamed piece at `offset`. Returns new received
    count. Raises OffsetMismatch (client resumes) or ValueError."""
    s = get_session(sid)
    if s is None:
        raise KeyError("unknown or expired upload")
    if offset != s["received"]:
        raise OffsetMismatch(s["received"])
    if s["received"] + len(piece) > s["size"]:
        raise ValueError("chunk exceeds the declared file size")
    if s["received"] + len(piece) > HARD_FILE_BYTES:
        raise ValueError("over the 1 GB hard limit")
    with open(s["tmp"], "r+b") as fh:
        fh.seek(offset)
        fh.write(piece)
    s["received"] += len(piece)
    return s["received"]


def finish_session(sid: str) -> dict | None:
    """Turn a complete session into a downloadable file record."""
    s = _SESSIONS.pop(sid, None)
    if s is None or s["received"] != s["size"]:
        return None
    fid = secrets.token_hex(12)
    final = os.path.join(UPLOAD_DIR, fid)
    os.replace(s["tmp"], final)
    expires = int(time.time()) + FILE_TTL
    _FILES[fid] = {"path": final, "name": s["name"], "size": s["size"],
                   "exp": expires, "used": False,
                   "uid": s.get("uid")}
    return {"fid": fid, "name": s["name"], "size": s["size"],
            "token": make_download_token(fid, expires)}


def lookup_file(fid: str) -> dict | None:
    """A live, unused file record — or None (used/expired/missing)."""
    f = _FILES.get(fid)
    if f is None or f["used"] or f["exp"] <= int(time.time()):
        return None
    if not os.path.exists(f["path"]):
        _drop_file(fid)
        return None
    return f


def consume(fid: str) -> dict | None:
    """Mark a file used and delete its disk copy. Called the moment
    the bot's Telegram upload succeeds — no waiting for expiry."""
    f = _FILES.pop(fid, None)
    if f:
        try:
            os.unlink(f["path"])
        except OSError:
            pass
    return f


def page_url(user_id: int | None = None) -> str:
    if not config.BASE_URL:
        return ""
    return config.BASE_URL + "/up/" + make_page_token(user_id)


def download_url(token: str) -> str:
    return (config.BASE_URL or "") + "/u/" + token


# ---------------------------------------------------------------- routes

async def _require_page(request: web.Request) -> dict:
    """Verify the page token; returns its payload (with "uid" when
    the /upage command knew who asked for the page)."""
    try:
        return verify_page_token(request.match_info["token"])
    except TokenError:
        raise web.HTTPGone(text="This upload link expired or is invalid. "
                                "Ask the bot for a fresh one with /upage.\n")


async def page(request: web.Request) -> web.Response:
    await _require_page(request)
    prune()
    html = PAGE_HTML.replace("__PAGE_TOKEN__", request.match_info["token"])
    return web.Response(text=html, content_type="text/html")


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def start(request: web.Request) -> web.Response:
    payload = await _require_page(request)
    body = await _json_body(request)
    try:
        out = start_session(body.get("name") or "file",
                            body.get("size") or 0,
                            uid=payload.get("uid"))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"sid": out["sid"],
                              "received": out["received"],
                              "chunk": CHUNK_SIZE})


async def find(request: web.Request) -> web.Response:
    await _require_page(request)
    body = await _json_body(request)
    found = find_session(body.get("name") or "file",
                         body.get("size") or 0)
    if found is None:
        return web.json_response({"error": "no session"}, status=404)
    return web.json_response(found)


async def status(request: web.Request) -> web.Response:
    await _require_page(request)
    s = get_session(request.query.get("sid", ""))
    if s is None:
        return web.json_response({"error": "no session"}, status=404)
    return web.json_response({"received": s["received"], "size": s["size"]})


async def abort(request: web.Request) -> web.Response:
    await _require_page(request)
    body = await _json_body(request)
    abort_session(body.get("sid") or "")
    return web.json_response({"ok": True})


async def chunk(request: web.Request) -> web.Response:
    await _require_page(request)
    sid = request.headers.get("X-Session", "")
    try:
        offset = int(request.headers.get("X-Offset", "-1"))
    except ValueError:
        return web.json_response({"error": "bad offset"}, status=400)
    s = get_session(sid)
    if s is None:
        return web.json_response({"error": "no session"}, status=404)
    if offset != s["received"]:
        return web.json_response({"received": s["received"]}, status=409)
    try:
        async for piece in request.content.iter_chunked(256 * 1024):
            if not piece:
                continue
            append_bytes(sid, offset, piece)
            offset += len(piece)
    except OffsetMismatch as exc:
        return web.json_response({"received": exc.received}, status=409)
    except ValueError as exc:
        abort_session(sid)
        return web.json_response({"error": str(exc)}, status=413)
    except KeyError:
        return web.json_response({"error": "no session"}, status=404)
    s = get_session(sid)
    if s is None:
        return web.json_response({"error": "no session"}, status=404)
    if s["received"] >= s["size"]:
        done = finish_session(sid)
        if done is None:
            return web.json_response({"error": "upload incomplete"},
                                     status=400)
        log.info("web upload finished: %s (%d bytes)",
                 done["name"], done["size"])
        return web.json_response({"received": s["size"], "done": True,
                                  "name": done["name"],
                                  "size": done["size"],
                                  "download_url": download_url(
                                      done["token"])})
    return web.json_response({"received": s["received"], "done": False})


def _download_token_from_body(body: dict) -> str:
    """The page JS sends the download token itself, or the full
    download URL it was given when the upload finished — accept
    either, so a future page change cannot break the button."""
    raw = (body.get("download_token") or body.get("download_url")
           or body.get("token") or "")
    if "/u/" in raw:
        raw = raw.rsplit("/u/", 1)[-1].split("?")[0].split("#")[0]
    return raw.strip()


async def to_admin(request: web.Request) -> web.Response:
    """"Upload to admin" button: upload a finished file to the
    owner's bot PM straight from disk, then delete the server
    copy (exactly like a successful link-send does). No Telegram
    link step is needed at all — that is the whole point.

    Safety, in order:
      * page token must be valid (link-holder only, 1 hour)
      * download token must point at a live, unused file
      * a second concurrent press for the same file is refused
        (409) instead of uploading twice
      * monthly bandwidth cap blocks the upload, WITHOUT
        consuming the file — the user can retry later
      * a failed Telegram upload leaves the file (and its link)
        intact for a retry, by link or by this button again
      * a successful upload consumes the file: disk copy deleted,
        download link stops working immediately
    """
    await _require_page(request)
    prune()
    body = await _json_body(request)
    try:
        fid = verify_download_token(_download_token_from_body(body))
    except TokenError:
        return web.json_response(
            {"error": "that download link is invalid or expired"},
            status=410)
    if fid in _ADMIN_SENDING:
        return web.json_response(
            {"error": "already uploading this file to the admin — "
                      "wait for it to finish"},
            status=409)
    rec = lookup_file(fid)
    if rec is None:
        return web.json_response(
            {"error": "that file is already sent, deleted, or expired"},
            status=410)
    if _bot is None:
        return web.json_response(
            {"error": "bot is not ready yet — try again in a moment"},
            status=503)
    from core import stats
    if stats.bandwidth_blocked():
        return web.json_response(
            {"error": stats.bandwidth_pause_text()}, status=429)
    path, name, size = rec["path"], rec["name"], rec["size"]
    if not os.path.exists(path):
        _drop_file(fid)
        return web.json_response(
            {"error": "that file is no longer on the server"}, status=410)
    _ADMIN_SENDING.add(fid)
    try:
        # Tell the owner who sent it, when known — otherwise the
        # file would arrive anonymously from a faceless page.
        uid = rec.get("uid")
        if uid and int(uid) != int(config.OWNER_ID):
            sender_label = None
            try:
                chat = await _bot.get_chat(int(uid))
                sender_label = getattr(chat, "first_name", None) \
                    or getattr(chat, "username", None)
            except Exception:
                sender_label = None
            try:
                await _bot.send_message(
                    config.OWNER_ID,
                    f"📤 Upage upload from "
                    f"{sender_label or 'a sudo user'} — file below:")
            except Exception:
                pass
        from core.sender import send_files
        await send_files(_bot, config.OWNER_ID, [path], caption=name)
    except Exception as exc:
        log.warning("page admin-upload failed for %s: %s", name, exc)
        return web.json_response(
            {"error": f"Telegram upload failed: {exc} — the file is "
                      f"still on the server, try again"},
            status=502)
    finally:
        _ADMIN_SENDING.discard(fid)
    consume(fid)
    log.info("page admin-upload sent: %s (%d bytes)", name, size)
    return web.json_response({"ok": True, "sent": True,
                              "name": name, "size": size})


async def delete_file(request: web.Request) -> web.Response:
    """Delete button: delete the disk file and expire its link
    immediately (the record is dropped, so the download link
    returns 410 from then on, even though its 1-hour token
    has not technically expired yet).

    Admin-send safety, as the owner asked:
      * if an admin upload for this file is IN PROGRESS, delete
        is refused (409) — deleting mid-upload would pull the
        file out from under it
      * if the file was already sent to the admin, it is already
        gone (consume deleted it) — delete reports that plainly
        instead of erroring
      * if the file has NOT been sent yet, the page JS asks for
        confirmation first, because deleting then means the
        admin will never receive it — the server itself still
        deletes on request, the confirmation is the guard
    """
    await _require_page(request)
    prune()
    body = await _json_body(request)
    try:
        fid = verify_download_token(_download_token_from_body(body))
    except TokenError:
        return web.json_response(
            {"error": "that download link is invalid or expired"},
            status=410)
    if fid in _ADMIN_SENDING:
        return web.json_response(
            {"error": "upload to admin is in progress for this file — "
                      "wait for it to finish, then delete if needed"},
            status=409)
    rec = _FILES.get(fid)
    if rec is None:
        return web.json_response(
            {"ok": True, "deleted": True,
             "note": "already sent to admin or expired — "
                     "server copy already gone"})
    name = rec["name"]
    _drop_file(fid)
    log.info("page delete: %s", name)
    return web.json_response({"ok": True, "deleted": True, "name": name})


async def serve_file(request: web.Request) -> web.Response:
    prune()
    try:
        fid = verify_download_token(request.match_info["token"])
    except TokenError:
        return web.Response(status=410, text="This download link "
                            "expired or is invalid.\n")
    rec = lookup_file(fid)
    if rec is None:
        return web.Response(status=410, text="This download link was "
                            "already used or expired — the file is gone "
                            "from the server.\n")
    headers = {
        "Content-Disposition":
            f"attachment; filename*=UTF-8''{quote(rec['name'])}",
    }
    # FileResponse streams from disk and answers Range/HEAD itself,
    # so nothing big is ever held in RAM.
    return web.FileResponse(rec["path"], headers=headers)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/up/{token}", page)
    app.router.add_post("/up/{token}/start", start)
    app.router.add_post("/up/{token}/find", find)
    app.router.add_get("/up/{token}/status", status)
    app.router.add_post("/up/{token}/abort", abort)
    app.router.add_post("/up/{token}/chunk", chunk)
    app.router.add_post("/up/{token}/to-admin", to_admin)
    app.router.add_post("/up/{token}/delete", delete_file)
    app.router.add_get("/u/{token}", serve_file)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>All-4-url Upload</title>
<style>
*{box-sizing:border-box}
body{margin:0 auto;max-width:720px;padding:20px 16px 32px;font-family:-apple-system,system-ui,Roboto,sans-serif;background:#0f1115;color:#e8e8e8;font-size:16px;line-height:1.5}
h1{font-size:24px;margin:0 0 6px}
.hint{color:#9aa0a6;font-size:15px;margin:0 0 20px}
.hint b{color:#e8e8e8}
#drop{border:2px dashed #3a3f4a;border-radius:16px;padding:36px 16px;text-align:center;color:#c8cdd4;font-size:17px}
#drop.over{border-color:#6a9bff;background:#1a2233}
#drop button{margin-top:16px;min-height:48px;padding:12px 24px;border:0;border-radius:12px;background:#2481cc;color:#fff;font-size:16px}
.row{margin-top:16px;padding:16px;border-radius:14px;background:#1b1e24}
.row .name{font-size:16px;font-weight:600;word-break:break-all}
.row .info{font-size:13px;color:#9aa0a6;margin-top:4px}
.bar{height:10px;background:#2a2e37;border-radius:5px;margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:#6a9bff}
.linkbox{margin-top:12px;padding:10px 12px;border-radius:10px;background:#0f1115;color:#6a9bff;word-break:break-all;font-size:13px}
.btnrow{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.btnrow button{min-height:44px;padding:10px 16px;border:0;border-radius:10px;font-size:15px}
.btnrow button:disabled{opacity:.5}
.btn-copy{background:#2a2e37;color:#e8e8e8}
.btn-admin{background:#2481cc;color:#fff;font-weight:600}
.btn-del{background:transparent;color:#ff7b7b;border:1px solid #ff7b7b}
.msg{font-size:14px;margin-top:10px}
.msg.ok{color:#7ee787}
.msg.err{color:#ff7b7b}
.msg.muted{color:#9aa0a6}
.foot{color:#777;font-size:13px;margin-top:24px}
</style>
</head>
<body>
<h1>📤 Private upload</h1>
<p class="hint">Drop files here or pick them. Max <b>750 MB</b> per file.
Uploads resume if your connection drops. When a file finishes, you get
three buttons: <b>Copy link</b>, <b>Upload to admin</b> (sends it straight
to the admin's bot PM — no link needed), and <b>Delete</b> (removes the
server copy and kills its link at once). This page sets no cookies, loads
nothing from anywhere else, and logs nothing about you.</p>
<div id="drop">Drop files here
<div><button id="pick">Choose files</button></div>
<input type="file" id="file" multiple hidden></div>
<div id="queue"></div>
<p class="foot">Links from here live 1 hour. Once a file is sent to the admin (or
to the bot via its link), its server copy is deleted and the link stops
working.</p>
<script>
const TOKEN = "__PAGE_TOKEN__";
const CHUNK = 16 * 1024 * 1024;
const MAX = 750 * 1024 * 1024;
const $ = id => document.getElementById(id);
const drop = $("drop"), input = $("file"), queueEl = $("queue");

function fmt(n){
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  return Math.round(n / 1024) + " KB";
}
async function api(path, opts){
  const r = await fetch("/up/" + TOKEN + path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
function setMsg(el, text, cls){
  el.textContent = text;
  el.className = "msg " + (cls || "");
}
function downloadToken(url){
  const i = url.indexOf("/u/");
  return i >= 0 ? url.slice(i + 3).split("?")[0].split("#")[0] : url;
}

async function uploadFile(file){
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = '<div class="name"></div><div class="info"></div>'
    + '<div class="bar"><i></i></div><div class="out"></div>';
  row.querySelector(".name").textContent = file.name + " (" + fmt(file.size) + ")";
  queueEl.appendChild(row);
  const bar = row.querySelector(".bar i");
  const info = row.querySelector(".info");
  const out = row.querySelector(".out");
  if (file.size > MAX){
    out.innerHTML = '<div class="msg err">Over the 750 MB limit — skipped.</div>';
    return;
  }
  let sid = null, received = 0;
  try {
    const f = await api("/find", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: file.name, size: file.size})});
    sid = f.sid; received = f.received;
  } catch (e) { /* no session yet */ }
  if (!sid){
    try {
      const s = await api("/start", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name: file.name, size: file.size})});
      sid = s.sid; received = s.received || 0;
    } catch (e) {
      out.innerHTML = '<div class="msg err">' + e.message + '</div>';
      return;
    }
  }
  const t0 = Date.now(), b0 = received;
  while (received < file.size){
    const end = Math.min(file.size, received + CHUNK);
    try {
      const res = await fetch("/up/" + TOKEN + "/chunk", {method: "POST",
        headers: {"X-Session": sid, "X-Offset": String(received)},
        body: file.slice(received, end)});
      const j = await res.json().catch(() => ({}));
      if (res.status === 409){ received = j.received; continue; }
      if (!res.ok) throw new Error(j.error || ("HTTP " + res.status));
      received = j.received;
      if (j.done){
        bar.style.width = "100%";
        info.textContent = "Done ✓ — " + fmt(j.size) + " — pick what to do with it:";
        const linkBox = document.createElement("div");
        linkBox.className = "linkbox";
        linkBox.textContent = j.download_url;
        const msg = document.createElement("div");
        msg.className = "msg";
        const btnRow = document.createElement("div");
        btnRow.className = "btnrow";
        const copy = document.createElement("button");
        copy.className = "btn-copy"; copy.textContent = "Copy link";
        copy.onclick = () => navigator.clipboard.writeText(j.download_url)
          .then(() => { copy.textContent = "Copied ✓"; })
          .catch(() => setMsg(msg, "Copy failed — long-press the link above to copy it.", "err"));
        const toAdmin = document.createElement("button");
        toAdmin.className = "btn-admin"; toAdmin.textContent = "Upload to admin";
        let adminSent = false;
        toAdmin.onclick = async () => {
          copy.disabled = true; toAdmin.disabled = true; del.disabled = true;
          setMsg(msg, "Sending to admin…", "muted");
          try {
            await api("/to-admin", {method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({download_token: downloadToken(j.download_url)})});
            adminSent = true;
            setMsg(msg, "✅ Sent to admin — server copy deleted, link expired.", "ok");
          } catch (e) {
            setMsg(msg, "❌ " + e.message, "err");
            copy.disabled = false; toAdmin.disabled = false; del.disabled = false;
          }
        };
        const del = document.createElement("button");
        del.className = "btn-del"; del.textContent = "Delete";
        del.onclick = async () => {
          if (!adminSent && !confirm("Delete this file from the server?

Its download link will stop working immediately, and the admin will NOT receive it unless you upload it again.")) return;
          copy.disabled = true; toAdmin.disabled = true; del.disabled = true;
          setMsg(msg, "Deleting…", "muted");
          try {
            const r = await api("/delete", {method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({download_token: downloadToken(j.download_url)})});
            setMsg(msg, r.note ? "🗑 " + r.note : "🗑 Deleted — server copy gone, link expired.", "ok");
          } catch (e) {
            setMsg(msg, "❌ " + e.message, "err");
            copy.disabled = false; toAdmin.disabled = false; del.disabled = false;
          }
        };
        btnRow.appendChild(copy); btnRow.appendChild(toAdmin); btnRow.appendChild(del);
        out.appendChild(linkBox); out.appendChild(btnRow); out.appendChild(msg);
        return;
      }
    } catch (e) {
      info.textContent = "Connection dropped — resuming… (" + e.message + ")";
      await sleep(2000);
      try {
        const st = await api("/status?sid=" + encodeURIComponent(sid));
        received = st.received;
      } catch (e2) { /* retry loop continues */ }
    }
    const pct = Math.round(received / file.size * 100);
    bar.style.width = pct + "%";
    const secs = (Date.now() - t0) / 1000;
    const speed = secs > 0 ? (received - b0) / secs : 0;
    const speedText = (speed / 1024 / 1024).toFixed(1) + " MB/s";
    let eta = "";
    if (speed > 0){
      const left = Math.round((file.size - received) / speed);
      eta = left < 60 ? " · ~" + left + " s left" : " · ~" + Math.round(left / 60) + " min left";
    }
    info.textContent = pct + "% · " + fmt(received) + " / " + fmt(file.size)
      + " · " + speedText + eta;
  }
}

let running = false;
async function runQueue(){
  if (running) return;
  running = true;
  while (queue.length){
    await uploadFile(queue.shift());
  }
  running = false;
}
const queue = [];
function addFiles(list){
  for (const f of list) queue.push(f);
  runQueue();
}
$("pick").onclick = () => input.click();
input.onchange = () => { addFiles(input.files); input.value = ""; };
drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", e => {
  e.preventDefault(); drop.classList.remove("over");
  addFiles(e.dataTransfer.files);
});
</script>
</body>
</html>
"""
