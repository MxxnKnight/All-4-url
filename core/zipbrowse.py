"""Zip file-manager page — browse an archive in the browser and
pick what to send (v1.51.14).

Moon's idea: the chat picker is fine for a few files, but tapping
through 100+ entries one by one is painful. A "Browse files"
button on the picker opens this page inside Telegram: a
file-manager view of the zip (thumbnails, name + extension,
size), with search, sort, folder tree, quick-select by type,
select-all, a live selected total, image preview, and two ways
out — "Send selected" (the usual one-by-one unpack) and "Send
as new zip" (re-pack just the picks into one zip).

Shape, following the panel (core/webapp.py):
  GET  /zip/{token}               the page (token in the URL)
  POST /api/zip/{token}/entries   the listing (initData + owner)
  GET  /api/zip/{token}/thumb/{i} a thumbnail (token is the bearer)
  GET  /api/zip/{token}/file/{i}  the extracted file (preview/download)
  POST /api/zip/{token}/send      {indices, mode} (initData + owner)

The token is HMAC-signed with the bot token (same shape as
core/linktoken.py) and carries the PENDING_UNZIP job id; it
expires with the job (1 hour, UNZIP_TTL). The record IS the
PENDING_UNZIP job dict (same object — no second copy to drift):
the picker registers it, the page reads it, the run drops it.
The send endpoint marks the job web_started so a later chat tap
can't start a second run of the same files.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import time

from aiohttp import web

import config
from core import unzip, webserver
from core.unpackrun import run_unpack
from core.utils import human_bytes, run_blocking
from core.webapp import validate_init_data

log = logging.getLogger("all4url")

#: Matches PENDING_UNZIP's TTL — the page dies with the job.
BROWSE_TTL = 3600
#: Thumbnails are downscaled to this size (longest side).
THUMB_SIZE = 320

#: job_id -> the PENDING_UNZIP job dict (same object, not a copy).
RECORDS: dict[str, dict] = {}

_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
_VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "m4v"}
_AUDIO_EXTS = {"mp3", "flac", "m4a", "opus", "ogg", "wav"}
_SUB_EXTS = {"srt", "ass", "ssa", "vtt", "sub"}


class BrowseError(Exception):
    """Bad token, expired page, or missing archive."""


def _secret() -> bytes:
    return (config.BOT_TOKEN or "all4url").encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(job_id: str) -> str:
    payload = {"j": job_id,
               "exp": int(time.time()) + BROWSE_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(),
                        hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str) -> str:
    """Token -> job id, or raise BrowseError."""
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise BrowseError("bad link")
    expected = _b64(hmac.new(_secret(), body.encode(),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise BrowseError("bad link")
    try:
        payload = json.loads(_unb64(body))
        job_id = str(payload["j"])
    except Exception:
        raise BrowseError("bad link")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise BrowseError("link expired")
    return job_id


def register(job: dict) -> None:
    """Index a picker job for the page. Called by
    _maybe_unzip_prompt when the picker goes on screen."""
    RECORDS[job["id"]] = job


def drop(job_id: str) -> None:
    RECORDS.pop(job_id, None)


def browse_url(job_id: str) -> str:
    """Full page URL for a picker job ("" when the server has no
    public URL — then the picker simply shows no Browse button)."""
    if not config.BASE_URL:
        return ""
    return f"{config.BASE_URL}/zip/{make_token(job_id)}"


def _record(token: str) -> dict:
    job_id = parse_token(token)
    job = RECORDS.get(job_id)
    if job is None:
        raise BrowseError("This browser expired — send the link again.")
    if job.get("web_started"):
        raise BrowseError("Already sending — check the chat.")
    if not os.path.isfile(job["path"]):
        raise BrowseError("The archive is gone — send the link again.")
    return job


def _kind(ext: str) -> str:
    ext = ext.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _SUB_EXTS:
        return "sub"
    return "other"


def _entries_payload(job: dict) -> dict:
    entries = job.get("entries") or []
    encrypted = set(job.get("encrypted") or [])
    out = []
    for i, (name, size) in enumerate(entries):
        base = name.rsplit("/", 1)[-1]
        folder = name.rsplit("/", 1)[0] if "/" in name else ""
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        out.append({
            "i": i, "name": name, "base": base, "dir": folder,
            "size": size, "size_h": human_bytes(size),
            "ext": ext, "kind": _kind(ext),
            "split": size > config.SPLIT_PART_BYTES,
            "enc": name in encrypted,
        })
    return {
        "ok": True,
        "archive": os.path.basename(job["path"]),
        "total": job.get("total", 0),
        "total_h": human_bytes(job.get("total", 0)),
        "count": len(out),
        "split_at": config.SPLIT_PART_BYTES,
        "split_at_h": human_bytes(config.SPLIT_PART_BYTES),
        "entries": out,
    }


async def _owner(request: web.Request):
    """(fields, body) for an owner-signed call, else (None, error)."""
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
                                   "open the page from the bot."},
            status=401)
    if fields["user_id"] != config.OWNER_ID:
        return None, None, web.json_response(
            {"ok": False, "error": "Owner only."}, status=403)
    return fields, body, None


async def page(request: web.Request) -> web.Response:
    try:
        _record(request.match_info["token"])
    except BrowseError as exc:
        return web.Response(text=str(exc), status=410,
                            content_type="text/plain")
    return web.Response(text=ZIP_HTML, content_type="text/html")


async def api_entries(request: web.Request) -> web.Response:
    _fields, _body, error = await _owner(request)
    if error is not None:
        return error
    try:
        job = _record(request.match_info["token"])
    except BrowseError as exc:
        return web.json_response({"ok": False, "error": str(exc)},
                                 status=410)
    return web.json_response(_entries_payload(job))


def _extract_one(job: dict, idx: int, dest_dir: str) -> str | None:
    """Extract one entry to dest_dir; return its path, or None.

    The generator deletes the file it yielded when it is closed
    (or on the next yield), so the bytes are copied somewhere
    stable before the generator is closed — returning the yielded
    path itself would hand back a file that is already gone."""
    entries = job.get("entries") or []
    if idx < 0 or idx >= len(entries):
        return None
    name = entries[idx][0]
    os.makedirs(dest_dir, exist_ok=True)
    skipped: list[str] = []
    gen = unzip.iter_extracted(job["path"], dest_dir, skipped,
                               {name})
    try:
        path = next(gen)
    except StopIteration:
        return None
    stable = os.path.join(dest_dir, "_one")
    try:
        shutil.copyfile(path, stable)
    except OSError:
        return None
    finally:
        gen.close()
    return stable


async def api_thumb(request: web.Request) -> web.Response:
    """A thumbnail for an image entry. Bearer auth is the token
    itself (same as file-to-link URLs) — <img> tags can't carry
    initData. Pillow downscales when available; without it the
    original file is served as-is."""
    try:
        job = _record(request.match_info["token"])
    except BrowseError as exc:
        return web.Response(text=str(exc), status=410)
    try:
        idx = int(request.match_info["idx"])
    except ValueError:
        return web.Response(text="bad index", status=400)
    entries = job.get("entries") or []
    if idx < 0 or idx >= len(entries):
        return web.Response(text="bad index", status=400)
    name, _size = entries[idx]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _IMAGE_EXTS:
        return web.Response(text="not an image", status=404)
    thumb_dir = os.path.join(job["job_dir"], ".thumbs")
    thumb_path = os.path.join(thumb_dir, f"{idx}.jpg")
    if not os.path.isfile(thumb_path):
        src_dir = os.path.join(job["job_dir"], ".thumb_src")
        src = _extract_one(job, idx, src_dir)
        if src is None or not os.path.isfile(src):
            return web.Response(text="could not extract", status=404)
        os.makedirs(thumb_dir, exist_ok=True)
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image is None:
            # No Pillow here (the Space has it via requirements):
            # serve the original bytes, the browser scales them.
            with open(src, "rb") as fh:
                data = fh.read()
            return web.Response(body=data,
                                content_type=f"image/{ext}")
        try:
            with Image.open(src) as im:
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                im.convert("RGB").save(thumb_path, "JPEG",
                                       quality=70)
        except Exception:
            return web.Response(text="could not thumbnail",
                                status=404)
    with open(thumb_path, "rb") as fh:
        return web.Response(body=fh.read(), content_type="image/jpeg")


async def api_vthumb(request: web.Request) -> web.Response:
    """One frame of a video entry, on demand (v1.51.15).

    Video thumbnails are NOT made up front — a season zip would
    make the page sit there extracting and frame-grabbing every
    video before it shows anything. The page shows 🎬 and only
    taps a frame when the owner taps that video's icon. The
    frame is made with the sender's own _make_thumb (ffmpeg,
    ~1s in), downscaled with Pillow into a disk cache, and
    served from the cache on repeat taps. The video itself is
    extracted only while ffmpeg runs and deleted right after —
    the generator would delete it on close anyway, so the frame
    is made before the generator is closed."""
    try:
        job = _record(request.match_info["token"])
    except BrowseError as exc:
        return web.Response(text=str(exc), status=410)
    try:
        idx = int(request.match_info["idx"])
    except ValueError:
        return web.Response(text="bad index", status=400)
    entries = job.get("entries") or []
    if idx < 0 or idx >= len(entries):
        return web.Response(text="bad index", status=400)
    name, _size = entries[idx]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _VIDEO_EXTS:
        return web.Response(text="not a video", status=404)
    vthumb_dir = os.path.join(job["job_dir"], ".vthumbs")
    cached = os.path.join(vthumb_dir, f"{idx}.jpg")
    if os.path.isfile(cached):
        with open(cached, "rb") as fh:
            return web.Response(body=fh.read(),
                                content_type="image/jpeg")
    src_dir = os.path.join(job["job_dir"], ".vthumb_src")
    os.makedirs(src_dir, exist_ok=True)
    skipped: list[str] = []
    gen = unzip.iter_extracted(job["path"], src_dir, skipped, {name})
    try:
        src = next(gen)
    except StopIteration:
        gen.close()
        return web.Response(text="could not extract", status=404)
    try:
        from core.sender import _make_thumb
        frame = await run_blocking(_make_thumb, src)
        if frame is None or not os.path.isfile(frame):
            return web.Response(text="no frame in this video",
                                status=404)
        os.makedirs(vthumb_dir, exist_ok=True)
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image is None:
            os.rename(frame, cached)
        else:
            try:
                with Image.open(frame) as im:
                    im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                    im.convert("RGB").save(cached, "JPEG", quality=70)
            except Exception:
                return web.Response(text="could not thumbnail",
                                    status=404)
            finally:
                try:
                    os.unlink(frame)
                except OSError:
                    pass
    finally:
        # Closes the generator, which deletes the extracted
        # video — the frame (a separate file) is already cached.
        gen.close()
    with open(cached, "rb") as fh:
        return web.Response(body=fh.read(), content_type="image/jpeg")


async def api_file(request: web.Request) -> web.Response:
    """The extracted file itself — image preview, or a per-file
    download from the page. Served from a disk cache so a second
    tap doesn't re-extract."""
    try:
        job = _record(request.match_info["token"])
    except BrowseError as exc:
        return web.Response(text=str(exc), status=410)
    try:
        idx = int(request.match_info["idx"])
    except ValueError:
        return web.Response(text="bad index", status=400)
    entries = job.get("entries") or []
    if idx < 0 or idx >= len(entries):
        return web.Response(text="bad index", status=400)
    cache_dir = os.path.join(job["job_dir"], ".cache")
    cached = os.path.join(cache_dir, str(idx))
    if not os.path.isfile(cached):
        path = _extract_one(job, idx, cache_dir)
        if path is None or not os.path.isfile(path):
            return web.Response(text="could not extract", status=404)
        os.rename(path, cached)
    name = entries[idx][0]
    base = name.rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    ctype = ("image/jpeg" if ext in ("jpg", "jpeg") else
             "image/png" if ext == "png" else
             "image/webp" if ext == "webp" else
             "image/gif" if ext == "gif" else
             "application/octet-stream")
    return web.FileResponse(cached, headers={
        "Content-Type": ctype,
        "Content-Disposition": f'inline; filename="{base}"'})


async def api_send(request: web.Request) -> web.Response:
    """{indices, mode} -> start the unpack run in the background
    and answer at once (the run takes minutes; progress lives in
    the chat's shared message). The picker is invalidated first,
    so a chat tap can't start a second run of the same files."""
    _fields, body, error = await _owner(request)
    if error is not None:
        return error
    try:
        job = _record(request.match_info["token"])
    except BrowseError as exc:
        return web.json_response({"ok": False, "error": str(exc)},
                                 status=410)
    mode = str(body.get("mode", "send"))
    if mode not in ("send", "rezip"):
        return web.json_response({"ok": False, "error": "bad mode"},
                                 status=400)
    entries = job.get("entries") or []
    encrypted = set(job.get("encrypted") or [])
    indices = []
    for raw in body.get("indices") or []:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(entries) and entries[i][0] not in encrypted:
            indices.append(i)
    if not indices:
        return web.json_response(
            {"ok": False, "error": "Tick at least one file first."},
            status=400)
    bot = webserver.get_bot()
    if bot is None:
        return web.json_response(
            {"ok": False, "error": "Bot is not running."}, status=503)
    job["web_started"] = True
    drop(job["id"])
    prompt_msg_id = job.get("prompt_msg_id")
    if prompt_msg_id:
        try:
            await bot.delete_messages(job["chat_id"], [prompt_msg_id])
        except Exception:
            pass
    names = {entries[i][0] for i in indices}
    total = sum(entries[i][1] for i in indices)
    loop = asyncio.get_running_loop()
    loop.create_task(run_unpack(
        bot, chat_id=job["chat_id"], zip_path=job["path"],
        job_dir=job["job_dir"], caption=job["caption"], names=names,
        user_id=config.OWNER_ID, mode=mode))
    log.info("zip browser: %s started for %d files (%s)",
             mode, len(indices), job["path"])
    return web.json_response({"ok": True, "mode": mode,
                              "count": len(indices), "total": total,
                              "total_h": human_bytes(total)})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/zip/{token}", page)
    app.router.add_post("/api/zip/{token}/entries", api_entries)
    app.router.add_get("/api/zip/{token}/thumb/{idx}", api_thumb)
    app.router.add_get("/api/zip/{token}/vthumb/{idx}", api_vthumb)
    app.router.add_get("/api/zip/{token}/file/{idx}", api_file)
    app.router.add_post("/api/zip/{token}/send", api_send)

ZIP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Browse archive</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg: var(--tg-theme-bg-color, #ffffff);
  --text: var(--tg-theme-text-color, #000000);
  --hint: var(--tg-theme-hint-color, #8a8a8e);
  --btn: var(--tg-theme-button-color, #2481cc);
  --btn-text: var(--tg-theme-button-text-color, #ffffff);
  --secondary: var(--tg-theme-secondary-bg-color, #f2f2f7);
  --link: var(--tg-theme-link-color, #2481cc);
  --danger: #e5484d;
}
*{box-sizing:border-box}
body{margin:0;padding:12px 12px 120px;font-family:-apple-system,system-ui,Roboto,sans-serif;
  background:var(--bg);color:var(--text)}
h1{font-size:17px;margin:2px 0 2px;word-break:break-all}
.sub{color:var(--hint);font-size:13px;margin-bottom:10px}
.toolbar{position:sticky;top:0;z-index:20;background:var(--bg);padding:6px 0 8px}
input[type=search],select{width:100%;border:1px solid rgba(127,127,127,.35);border-radius:10px;
  background:var(--bg);color:var(--text);padding:10px;font-size:14px}
select{margin-top:8px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{border:1px solid rgba(127,127,127,.35);border-radius:999px;padding:6px 11px;
  font-size:13px;background:var(--bg);color:var(--text)}
.chip.on{background:var(--btn);color:var(--btn-text);border-color:var(--btn)}
.folder{margin-top:10px}
.folder>button{width:100%;display:flex;justify-content:space-between;align-items:center;
  border:0;background:var(--secondary);color:var(--text);border-radius:10px;
  padding:10px 12px;font-size:14px;font-weight:600;text-align:left}
.folder>button span:last-child{color:var(--hint);font-weight:400;font-size:12.5px}
.row{display:flex;align-items:center;gap:10px;padding:8px 4px;
  border-bottom:1px solid rgba(127,127,127,.18)}
.row input[type=checkbox]{width:20px;height:20px;flex:none;accent-color:var(--btn)}
.row .thumb{width:44px;height:44px;flex:none;border-radius:8px;object-fit:cover;
  background:var(--secondary)}
.row .ic{width:44px;height:44px;flex:none;border-radius:8px;background:var(--secondary);
  display:flex;align-items:center;justify-content:center;font-size:20px}
.row .ic.vthumb{cursor:pointer}
.row .nm{flex:1;min-width:0}
.row .nm b{display:block;font-size:14px;font-weight:500;word-break:break-all}
.row .nm span{font-size:12.5px;color:var(--hint)}
.row .sz{font-size:12.5px;color:var(--hint);white-space:nowrap;text-align:right}
.badge{display:inline-block;font-size:11px;color:var(--danger);font-weight:600}
.row.enc{opacity:.55}
.footer{position:fixed;left:0;right:0;bottom:0;z-index:30;background:var(--bg);
  border-top:1px solid rgba(127,127,127,.3);padding:10px 12px calc(10px + env(safe-area-inset-bottom))}
.footer .sum{font-size:13.5px;margin-bottom:8px}
.footer .warn{font-size:12.5px;color:var(--danger);margin-bottom:8px}
.footer .btns{display:flex;gap:8px}
.footer button{flex:1;padding:12px;border:0;border-radius:10px;font-size:15px;font-weight:600}
.footer .send{background:var(--btn);color:var(--btn-text)}
.footer .rezip{background:var(--secondary);color:var(--text)}
.footer button:disabled{opacity:.45}
.msg{font-size:13px;margin-top:8px;min-height:16px}
.msg.err{color:var(--danger)}.msg.ok{color:#30a46c}
.empty{color:var(--hint);text-align:center;padding:30px 10px;font-size:14px}
#preview{position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.92);
  display:none;align-items:center;justify-content:center;padding:10px}
#preview img{max-width:100%;max-height:100%;object-fit:contain}
#preview .x{position:absolute;top:14px;right:16px;color:#fff;font-size:26px;
  background:rgba(0,0,0,.4);border-radius:50%;width:38px;height:38px;
  display:flex;align-items:center;justify-content:center}
</style>
</head>
<body>
<h1 id="title">📦 Loading…</h1>
<div class="sub" id="sub"></div>
<div class="toolbar">
  <input type="search" id="q" placeholder="Search name or extension…" autocomplete="off">
  <select id="sort">
    <option value="name">Sort: name</option>
    <option value="size">Sort: size (big first)</option>
  </select>
  <div class="chips" id="chips">
    <button class="chip" data-k="image">🖼 Images</button>
    <button class="chip" data-k="video">🎬 Videos</button>
    <button class="chip" data-k="sub">💬 Subtitles</button>
    <button class="chip" data-k="all">✅ All shown</button>
    <button class="chip" data-k="none">⬜ None</button>
  </div>
</div>
<div id="list"></div>
<div class="msg" id="msg"></div>
<div class="footer">
  <div class="sum" id="sum">Nothing selected</div>
  <div class="warn" id="warn" hidden></div>
  <div class="btns">
    <button class="send" id="sendBtn" disabled>📤 Send selected</button>
    <button class="rezip" id="rezipBtn" disabled>📦 Send as new zip</button>
  </div>
</div>
<div id="preview"><span class="x" id="prevClose">✕</span><img id="prevImg" alt=""></div>
<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
function readInitData() {
  try { if (tg && tg.initData) return tg.initData; } catch (e) {}
  try {
    const m = location.hash.match(/tgWebAppData=([^&]+)/);
    if (m) return decodeURIComponent(m[1]);
  } catch (e) {}
  return "";
}
let initData = readInitData();
const $ = id => document.getElementById(id);
const token = location.pathname.split("/").pop();
const KIND_ICON = {video: "🎬", audio: "🎵", sub: "💬", other: "📄"};
let DATA = null;          // entries payload from the server
let sel = new Set();      // selected entry indices
let openFolders = new Set([""]);  // "" = root, always open
let vloaded = new Set();  // video indices whose frame thumb loaded
let vfailed = new Set();  // video indices with no frame (don't retry)

function fmt(n) {
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}
async function api(path, extra) {
  try {
    const r = await fetch(path, {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.assign({initData}, extra || {}))});
    try { return await r.json(); }
    catch (e) { return {ok: false, error: "Server answered " + r.status + " (not JSON)."}; }
  } catch (e) { return {ok: false, error: "Could not reach the server: " + e.message}; }
}
function visible() {
  const q = $("q").value.trim().toLowerCase();
  let list = DATA.entries.filter(e =>
    !q || e.name.toLowerCase().includes(q) || ("." + e.ext).includes(q));
  if ($("sort").value === "size") list = list.slice().sort((a, b) => b.size - a.size);
  else list = list.slice().sort((a, b) => a.name.localeCompare(b.name));
  return list;
}
function render() {
  const list = visible();
  const byDir = {};
  list.forEach(e => { (byDir[e.dir] = byDir[e.dir] || []).push(e); });
  const dirs = Object.keys(byDir).sort();
  let html = "";
  if (!list.length) html = `<div class="empty">No files match.</div>`;
  for (const dir of dirs) {
    const rows = byDir[dir];
    const open = openFolders.has(dir);
    if (dir) {
      html += `<div class="folder"><button data-dir="${dir}">📁 ${dir} <span>${rows.length} file(s) ${open ? "▾" : "▸"}</span></button>`;
    }
    if (open) {
      for (const e of rows) {
        let thumb;
        if (e.kind === "image") {
          thumb = `<img class="thumb" loading="lazy" src="/api/zip/${token}/thumb/${e.i}" data-full="/api/zip/${token}/file/${e.i}" alt="">`;
        } else if (e.kind === "video") {
          // Frames are made on demand, one tap at a time — never
          // up front for the whole zip (v1.51.15).
          thumb = vloaded.has(e.i)
            ? `<img class="thumb" src="/api/zip/${token}/vthumb/${e.i}" data-full="/api/zip/${token}/vthumb/${e.i}" alt="">`
            : `<span class="ic vthumb" data-vt="${e.i}" title="Tap for a preview frame">🎬</span>`;
        } else {
          thumb = `<span class="ic">${KIND_ICON[e.kind] || "📄"}</span>`;
        }
        html += `<div class="row${e.enc ? " enc" : ""}">
          <input type="checkbox" data-i="${e.i}" ${sel.has(e.i) ? "checked" : ""} ${e.enc ? "disabled" : ""}>
          ${thumb}
          <div class="nm"><b>${e.base}</b><span>${e.ext ? "." + e.ext + " · " : ""}${e.size_h}</span></div>
          <div class="sz">${e.split ? `<span class="badge">⚠️ splits</span><br>` : ""}${e.size_h}</div>
        </div>`;
      }
    }
    if (dir) html += `</div>`;
  }
  $("list").innerHTML = html;
  updateSum();
}
function updateSum() {
  let n = 0, total = 0, splitN = 0;
  sel.forEach(i => {
    const e = DATA.entries[i];
    if (!e) return;
    n++; total += e.size;
    if (e.split) splitN++;
  });
  $("sum").textContent = n ? `${n} file(s) selected · ${fmt(total)}` : "Nothing selected";
  const w = $("warn");
  if (splitN) { w.hidden = false; w.textContent = `⚠️ ${splitN} file(s) over ${DATA.split_at_h} will arrive split into parts.`; }
  else w.hidden = true;
  $("sendBtn").disabled = $("rezipBtn").disabled = !n;
}
$("list").addEventListener("click", e => {
  const f = e.target.closest("[data-dir]");
  if (f) {
    const d = f.dataset.dir;
    openFolders.has(d) ? openFolders.delete(d) : openFolders.add(d);
    render();
    return;
  }
  const vt = e.target.closest("[data-vt]");
  if (vt) {
    const i = +vt.dataset.vt;
    if (vfailed.has(i)) return;
    const img = document.createElement("img");
    img.className = "thumb";
    img.src = `/api/zip/${token}/vthumb/${i}`;
    img.setAttribute("data-full", img.src);
    img.alt = "";
    img.onload = () => vloaded.add(i);
    img.onerror = () => {
      vfailed.add(i);
      const s = document.createElement("span");
      s.className = "ic vthumb"; s.dataset.vt = i;
      s.title = "No preview for this video";
      s.textContent = "🎬";
      img.replaceWith(s);
    };
    vt.replaceWith(img);
    return;
  }
  const p = e.target.closest("[data-full]");
  if (p) {
    $("prevImg").src = p.dataset.full;
    $("preview").style.display = "flex";
    return;
  }
});
$("list").addEventListener("change", e => {
  const cb = e.target.closest("input[type=checkbox][data-i]");
  if (!cb) return;
  const i = +cb.dataset.i;
  cb.checked ? sel.add(i) : sel.delete(i);
  updateSum();
});
$("q").addEventListener("input", render);
$("sort").addEventListener("change", render);
$("chips").addEventListener("click", e => {
  const c = e.target.closest("[data-k]");
  if (!c) return;
  const k = c.dataset.k;
  if (k === "none") sel.clear();
  else if (k === "all") visible().forEach(en => { if (!en.enc) sel.add(en.i); });
  else DATA.entries.forEach(en => { if (en.kind === k && !en.enc) sel.add(en.i); });
  render();
});
$("prevClose").onclick = () => { $("preview").style.display = "none"; $("prevImg").src = ""; };
$("preview").addEventListener("click", e => {
  if (e.target.id === "preview") { $("preview").style.display = "none"; $("prevImg").src = ""; }
});
async function send(mode) {
  const m = $("msg"); m.className = "msg"; m.textContent = "Starting…";
  const r = await api(`/api/zip/${token}/send`,
    {indices: Array.from(sel), mode});
  if (r.ok) {
    m.className = "msg ok";
    m.textContent = `✅ Sending ${r.count} file(s) (${r.total_h}) — watch the chat.`;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    setTimeout(() => { if (tg) tg.close(); }, 1200);
  } else { m.className = "msg err"; m.textContent = "❌ " + (r.error || "failed"); }
}
$("sendBtn").onclick = () => send("send");
$("rezipBtn").onclick = () => send("rezip");
async function load() {
  if (!initData) {
    await new Promise(r => setTimeout(r, 1000));
    initData = readInitData();
  }
  if (!initData) {
    $("title").textContent = "Open this page from inside Telegram — tap the bot's 🔍 Browse files button. As a plain web link it carries no Telegram signature.";
    return;
  }
  const d = await api(`/api/zip/${token}/entries`);
  if (!d.ok) { $("title").textContent = d.error || "Could not load."; return; }
  DATA = d;
  $("title").textContent = "📦 " + d.archive;
  $("sub").textContent = `${d.count} file(s) · unpacks to ${d.total_h}`
    + (d.entries.some(e => e.kind === "video") ? " · tap 🎬 for a video frame" : "");
  render();
}
load();
</script>
</body>
</html>
"""
