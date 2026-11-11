"""Permanent private link-upload page (the /sendlink command).

Different from /upage on purpose:
  * /upage = upload a FILE from your device, link expires in 1 hour.
  * /sendlink = paste a URL on a web page, the server downloads it
    and uploads it to the owner's bot PM. The page link NEVER
    expires — it is a permanent, unlisted page: only people who
    have the link can open it, and it is marked noindex/nofollow
    (meta tag, X-Robots-Tag header, robots.txt) so Google never
    lists it. There is no password; the link itself is the key,
    so treat it like one — anyone holding it can make the server
    download a link and spend the owner's bandwidth, exactly like
    a sudo user pasting a link in chat.

Page layout (the owner's one fix on the mockup): the paste box
and Fetch button are at the TOP, above everything else — no
hero text to scroll past. The explanation sits below the work
area, short.

Flow: paste link -> Fetch (server resolves it, no download yet)
-> pick a quality (best is pre-selected, so one tap on Upload
is enough) -> Upload to Owner -> the server downloads it
(best quality by default, or the picked format) and sends it
to the owner's bot PM, then deletes the server copy. The page
polls for progress; the job runs in the background, so a slow
download never hits an HTTP timeout.

No quality picker exists for links with no formats to pick
from (direct files, Terabox, custom sites): those offer Best
quality + Document only. Terabox shares with several files
take the largest file. Zips are sent as-is (the owner can
unzip them with the bot later).

Same download logic as the bot's chat uploader, in the same
order (plugins/uploader.py::_dispatch): Terabox -> custom
sites -> Instagram carousel -> direct file -> yt-dlp ->
gallery-dl -> page scrape. The only differences are the
web format picker (instead of the chat picker) and no unzip
prompt. Images work the same way too: direct image links
come down as photos, Instagram posts as their carousel
images, Reddit / Imgur / image-board pages via gallery-dl.
No cookies, no external resources, no IP/user-agent logging
— same privacy as /upage.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import urlparse

from aiohttp import web

import config
from core import stats
from core.extractors import gallery_engine, scrape, site_registry, terabox
from core.extractors.direct import check_direct, download_file, is_playlist_url
from core.extractors.ytdlp_engine import (
    YtdlpError, best_formats, carousel_image_urls,
    download as ytdlp_download, extract_info)
from core.utils import (
    JOB_SEMAPHORE, SizeBlocked, check_size, cleanup_dir, human_bytes,
    make_job_dir)

log = logging.getLogger("all4url.sendlink")

#: Job records live 1 hour; a fetched link waits 10 minutes for a
#: format pick before the job is dropped (resubmit the link then).
JOB_TTL = 3600
PICK_TTL = 600

_bot = None  # set by webserver.start_web_server() via bind_bot()


def bind_bot(client) -> None:
    global _bot
    _bot = client


class TokenError(Exception):
    """Bad signature or malformed token."""


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
    """Like upage's verify, but with NO expiry — that is the whole
    point of this page. A token is valid forever unless the bot
    token itself changes."""
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise TokenError("malformed token")
    expected = _b64(hmac.new(_secret(domain), body.encode(),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise TokenError("bad signature")
    try:
        return json.loads(_unb64(body))
    except Exception:
        raise TokenError("malformed payload")


def make_page_token(user_id: int | None = None) -> str:
    payload = {"r": secrets.token_hex(16)}
    if user_id:
        payload["uid"] = int(user_id)
    return _sign("sendlink-page", payload)


def verify_page_token(token: str) -> dict:
    return _verify("sendlink-page", token)


def page_url(user_id: int | None = None) -> str:
    if not config.BASE_URL:
        return ""
    return config.BASE_URL + "/sendlink/" + make_page_token(user_id)


# ---------------------------------------------------------------- jobs
# In-memory on purpose: a job only lives while its download is on
# this instance's ephemeral disk. A restart loses the disk anyway.
_JOBS: dict[str, dict] = {}  # job_id -> job record (see _new_job)


def _new_job(url: str, uid: int | None) -> dict:
    job_id = secrets.token_hex(8)
    _JOBS[job_id] = {
        "id": job_id, "url": url, "uid": uid,
        "stage": "resolving", "title": "", "done": 0, "total": 0,
        "formats": [], "picked": None, "result": None, "error": None,
        "created": int(time.time()),
        "pick_event": asyncio.Event(),
        "resolved": None,
    }
    return _JOBS[job_id]


def _prune() -> None:
    now = int(time.time())
    for job_id, job in list(_JOBS.items()):
        if job["created"] + JOB_TTL <= now:
            _JOBS.pop(job_id, None)
        elif job["stage"] == "awaiting_pick" \
                and job["created"] + PICK_TTL <= now:
            job["stage"] = "failed"
            job["error"] = ("nobody picked a format in time — "
                            "paste the link again")


def _public(job: dict) -> dict:
    """What the page is allowed to see — no internal state, no uid."""
    return {"stage": job["stage"], "title": job.get("title") or "",
            "done": job.get("done") or 0, "total": job.get("total") or 0,
            "formats": job.get("formats") or [],
            "result": job.get("result"),
            "error": job.get("error")}


def _fmt_choices(formats: list[dict], ytdlp: bool) -> list[dict]:
    """Picker choices for the page. Best is always first (and the
    page pre-selects it); Document is a send-mode, not a format —
    the file is downloaded at best quality and sent as a document
    instead of a video."""
    choices = [{"id": "best", "label": "⭐ Best quality"}]
    for fmt in (formats or [])[:5]:
        size = f" · {human_bytes(fmt['filesize'])}" if fmt.get("filesize") else ""
        choices.append({"id": fmt["format_id"],
                        "label": f"{fmt['height']}p{size}"})
    if ytdlp:
        choices.append({"id": "audio", "label": "🎧 Audio only (mp3)"})
    choices.append({"id": "doc", "label": "📄 Document"})
    return choices


async def _resolve(url: str) -> dict:
    """Work out what a link is WITHOUT downloading it yet. Returns
    {kind, title, size, formats, ...} for the download step, or
    raises RuntimeError with the plain reason. Order mirrors the
    bot's resolver chain in plugins/uploader.py."""
    errors: list[str] = []
    # 1) Terabox — largest file wins (no picker on a web page).
    if terabox.is_terabox_url(url):
        try:
            files = await terabox.list_files(url)
            if not files:
                raise RuntimeError("that Terabox share looks empty")
            biggest = max(files, key=lambda f: f.get("size") or 0)
            check_size(biggest.get("size") or 0,
                       biggest.get("name") or "file")
            return {"kind": "terabox", "url": url,
                    "title": biggest.get("name") or "file",
                    "size": biggest.get("size") or 0,
                    "index": files.index(biggest),
                    "via": biggest.get("via")}
        except (SizeBlocked, RuntimeError):
            raise
        except Exception as exc:
            errors.append(f"terabox: {exc}")
    # 2) Custom-site extractors (luluvido etc.).
    site = site_registry.match(url)
    if site:
        try:
            info = await site.resolve(url)
            if info.get("approx_size"):
                check_size(info["approx_size"],
                           info.get("title") or "file")
            return {"kind": "custom", "url": url, "site": site,
                    "title": info.get("title") or "file",
                    "size": info.get("approx_size") or 0}
        except SizeBlocked:
            raise
        except Exception as exc:
            errors.append(f"{site.__name__.rsplit('.', 1)[-1]}: {exc}")
    # 2b) Instagram photo posts — same as the bot's _instagram_carousel:
    # yt-dlp metadata carries every carousel image URL, so the post
    # is downloaded as images, not as a video. No carousel found?
    # Fall through — gallery-dl gets its turn at step 5.
    if "instagram.com/p/" in url:
        try:
            info = await extract_info(url, ignore_no_formats=True)
            urls = carousel_image_urls(info)
            if urls:
                return {"kind": "carousel", "url": url, "urls": urls,
                        "title": info.get("title") or "Instagram post",
                        "size": 0}
            errors.append("instagram: no images found in that post")
        except Exception as exc:
            errors.append(f"instagram: {exc}")
    # 3) A raw file link needs no extractor at all.
    try:
        direct_info = await check_direct(url)
    except Exception as exc:
        direct_info = None
        errors.append(f"direct: {exc}")
    if direct_info:
        try:
            check_size(direct_info.get("size") or 0,
                       direct_info.get("filename") or "file")
        except SizeBlocked:
            raise
        return {"kind": "direct", "url": url,
                "final_url": direct_info.get("final_url") or url,
                "title": direct_info.get("filename") or "file",
                "size": direct_info.get("size") or 0}
    # 4) yt-dlp — the 1000+ sites it understands.
    try:
        info = await extract_info(url)
        title = info.get("title") or "Download"
        formats = best_formats(info) if info.get("formats") else []
        if formats:
            check_size(formats[0].get("filesize") or 0, title)
        size = formats[0].get("filesize") or 0 if formats else 0
        return {"kind": "ytdlp", "url": url, "title": title,
                "size": size, "formats": formats}
    except SizeBlocked:
        raise
    except Exception as exc:
        errors.append(f"yt-dlp: {exc}")
    # 5) gallery-dl covers image hosts and galleries yt-dlp does
    # not (Reddit posts, Imgur albums, image boards…). It only
    # proves itself by downloading, so resolve hands the job to it
    # and the download step tries it — with the page-scrape as its
    # own fallback, exactly like the bot's chain.
    return {"kind": "gallery", "url": url, "title": url, "size": 0,
            "errors": errors}


async def _download_resolved(resolved: dict, picked: str, job_dir: str,
                             progress) -> list[str]:
    """Download the resolved link with the picked choice. `picked`
    is a format id, "best", "audio" (yt-dlp only) or "doc"
    (best quality, sent as a document — handled at send time)."""
    kind = resolved["kind"]
    url = resolved["url"]
    if kind == "terabox":
        path = await terabox.download(url, job_dir,
                                      index=resolved["index"],
                                      progress_cb=progress)
        return [path]
    if kind == "custom":
        return await resolved["site"].download(url, job_dir,
                                               progress_cb=progress)
    if kind == "carousel":
        # Instagram post: every image URL from the metadata, saved
        # as instagram_1.jpg, instagram_2.jpg… — send_files groups
        # them as a photo set, exactly like the bot does.
        paths = []
        for i, img_url in enumerate(resolved["urls"]):
            paths.append(await download_file(
                img_url, job_dir, progress_cb=progress,
                filename=f"instagram_{i + 1}.jpg"))
        return paths
    if kind == "direct":
        path = await download_file(resolved["final_url"], job_dir,
                                   progress_cb=progress,
                                   filename=resolved.get("title"))
        return [path]
    if kind == "ytdlp":
        if picked == "audio":
            return await ytdlp_download(url, job_dir, audio_only=True,
                                        progress_cb=progress)
        fmt = None if picked in ("best", "doc") else picked
        return await ytdlp_download(url, job_dir, format_id=fmt,
                                    progress_cb=progress)
    if kind == "gallery":
        # gallery-dl first (Reddit, Imgur, image boards…); if it
        # finds nothing, the page-scrape is the last resort — same
        # as the bot's chain, including the stream-playlist detour
        # (a playlist goes to yt-dlp, not to a text file).
        try:
            return await gallery_engine.download(url, job_dir)
        except Exception as gal_exc:
            try:
                media_url = await scrape.find_media_url(url)
                if is_playlist_url(media_url):
                    return await ytdlp_download(media_url, job_dir,
                                                progress_cb=progress)
                path = await scrape.download(url, job_dir,
                                             progress_cb=progress)
                return [path]
            except Exception as scrape_exc:
                prior = "; ".join(resolved.get("errors") or [])
                raise RuntimeError(
                    "Couldn't figure out that link — "
                    + (prior + "; " if prior else "")
                    + f"gallery-dl: {gal_exc}; scrape: {scrape_exc}")
    raise RuntimeError(f"unknown resolved kind: {kind}")


async def _send_to_owner(paths: list[str], title: str,
                         uid: int | None, as_document: bool) -> None:
    """Deliver finished files to the owner's bot PM, bypassing
    /destination — same rule as upage admin delivery."""
    if _bot is None:
        raise RuntimeError("bot is not ready yet — try again in a moment")
    if uid and int(uid) != int(config.OWNER_ID):
        try:
            chat = await _bot.get_chat(int(uid))
            label = getattr(chat, "first_name", None) \
                or getattr(chat, "username", None) or "a user"
        except Exception:
            label = "a user"
        try:
            await _bot.send_message(
                config.OWNER_ID,
                f"📤 Link upload from {label} — file below:")
        except Exception:
            pass
    if as_document:
        sent_any = False
        for i, path in enumerate(paths):
            sent = await _bot.send_document(
                config.OWNER_ID, path,
                caption=title[:200] if i == 0 else "")
            sent_any = sent_any or sent is not None
        if not sent_any:
            raise RuntimeError("Telegram returned nothing for the file")
        stats.record_paths("uploaded", paths)
        return
    from core.sender import send_files
    sent = await send_files(_bot, config.OWNER_ID, paths,
                            caption=title[:200])
    if not sent:
        raise RuntimeError("Telegram returned nothing for the file")


async def _run_job(job_id: str) -> None:
    """Background job: resolve -> wait for a format pick -> download
    -> send to owner -> delete the server copy. The page polls the
    job record; this task never holds an HTTP request open."""
    job = _JOBS.get(job_id)
    if job is None:
        return
    url, uid = job["url"], job["uid"]
    try:
        if stats.bandwidth_blocked():
            raise RuntimeError(stats.bandwidth_pause_text())
        job["stage"] = "resolving"
        resolved = await _resolve(url)
        job["resolved"] = resolved
        job["title"] = resolved.get("title") or "Download"
        job["total"] = resolved.get("size") or 0
        job["formats"] = _fmt_choices(resolved.get("formats") or [],
                                      resolved["kind"] == "ytdlp")
        job["stage"] = "awaiting_pick"
        try:
            await asyncio.wait_for(job["pick_event"].wait(),
                                   timeout=PICK_TTL)
        except asyncio.TimeoutError:
            raise RuntimeError("nobody picked a format in time — "
                               "paste the link again")
        picked = job["picked"] or "best"
        job["stage"] = "downloading"

        async def progress(done, total):
            job["done"] = done
            job["total"] = total or job["total"]

        async with JOB_SEMAPHORE:
            job_dir = make_job_dir()
            try:
                paths = await _download_resolved(resolved, picked,
                                                 job_dir, progress)
                job["stage"] = "uploading"
                job["done"] = job["total"]
                await _send_to_owner(paths, job["title"], uid,
                                     as_document=(picked == "doc"))
                total_bytes = sum(
                    os.path.getsize(p) for p in paths
                    if os.path.exists(p))
                job["result"] = {
                    "name": os.path.basename(paths[0]) if paths else job["title"],
                    "size": total_bytes,
                    "count": len(paths)}
                job["stage"] = "done"
                stats.record_job("sendlink", ok=True)
            finally:
                cleanup_dir(job_dir)
    except (SizeBlocked, RuntimeError, YtdlpError) as exc:
        job["stage"] = "failed"
        job["error"] = str(exc)[:500]
        stats.record_job("sendlink", ok=False)
    except Exception as exc:
        log.warning("sendlink job %s failed: %s", job_id, exc)
        job["stage"] = "failed"
        job["error"] = str(exc)[:500]
        stats.record_job("sendlink", ok=False)


# ---------------------------------------------------------------- routes

async def _require_page(request: web.Request) -> dict:
    try:
        return verify_page_token(request.match_info["token"])
    except TokenError:
        raise web.HTTPGone(text="This page link is invalid. Ask the bot "
                                "for a fresh one with /sendlink.\n")


def _noindex_headers() -> dict:
    return {"X-Robots-Tag": "noindex, nofollow"}


async def page(request: web.Request) -> web.Response:
    await _require_page(request)
    _prune()
    html = PAGE_HTML.replace("__PAGE_TOKEN__",
                             request.match_info["token"])
    return web.Response(text=html, content_type="text/html",
                        headers=_noindex_headers())


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def submit(request: web.Request) -> web.Response:
    payload = await _require_page(request)
    _prune()
    body = await _json_body(request)
    url = (body.get("url") or "").strip()
    if not url or len(url) > 2000:
        return web.json_response({"error": "paste a link first"},
                                 status=400)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return web.json_response(
            {"error": "that doesn't look like a web link — "
                      "it needs to start with http:// or https://"},
            status=400)
    if _bot is None:
        return web.json_response(
            {"error": "bot is not ready yet — try again in a moment"},
            status=503)
    job = _new_job(url, payload.get("uid"))
    asyncio.create_task(_run_job(job["id"]))
    return web.json_response({"job": job["id"]})


async def status(request: web.Request) -> web.Response:
    await _require_page(request)
    _prune()
    job = _JOBS.get(request.query.get("job", ""))
    if job is None:
        return web.json_response({"error": "job not found — it may have "
                                           "expired, paste the link again"},
                                 status=404)
    return web.json_response(_public(job))


async def pick(request: web.Request) -> web.Response:
    await _require_page(request)
    _prune()
    body = await _json_body(request)
    job = _JOBS.get(body.get("job") or "")
    if job is None:
        return web.json_response({"error": "job not found"}, status=404)
    if job["stage"] != "awaiting_pick":
        return web.json_response(
            {"error": "that job isn't waiting for a format pick"},
            status=409)
    choice = body.get("choice") or "best"
    valid = {f["id"] for f in job.get("formats") or []} | {"best"}
    if choice not in valid:
        return web.json_response({"error": "unknown format choice"},
                                 status=400)
    job["picked"] = choice
    job["pick_event"].set()
    return web.json_response({"ok": True})


async def robots(request: web.Request) -> web.Response:
    return web.Response(text="User-agent: *\n"
                             "Disallow: /up/\n"
                             "Disallow: /u/\n"
                             "Disallow: /d/\n"
                             "Disallow: /stream/\n"
                             "Disallow: /sendlink/\n",
                        content_type="text/plain")


def add_routes(app: web.Application) -> None:
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/sendlink/{token}", page)
    app.router.add_post("/sendlink/{token}/submit", submit)
    app.router.add_get("/sendlink/{token}/status", status)
    app.router.add_post("/sendlink/{token}/pick", pick)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>SendByLink — private link upload</title>
<style>
*{box-sizing:border-box}
body{margin:0 auto;max-width:720px;padding:20px 16px 32px;font-family:-apple-system,system-ui,Roboto,sans-serif;background:#0f1115;color:#e8e8e8;font-size:16px;line-height:1.5}
.top{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.logo{width:44px;height:44px;border-radius:12px;background:#e8e8e8;color:#0f1115;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:700}
.top h1{font-size:20px;margin:0}
.top .sub{font-size:13px;color:#9aa0a6}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.badges span{font-size:13px;padding:6px 12px;border-radius:999px;background:#1b1e24;color:#9aa0a6}
.badges .perm{background:#123324;color:#7ee787}
.card{background:#1b1e24;border-radius:16px;padding:16px}
.card label{display:block;font-size:13px;letter-spacing:.08em;color:#9aa0a6;margin-bottom:8px}
.urlrow{display:flex;gap:8px}
.urlrow input{flex:1;min-width:0;height:52px;padding:0 14px;border:1px solid #3a3f4a;border-radius:12px;background:#0f1115;color:#e8e8e8;font-size:16px}
.urlrow button{height:52px;padding:0 16px;border:1px solid #3a3f4a;border-radius:12px;background:#2a2e37;color:#e8e8e8;font-size:15px}
.fetch{width:100%;margin-top:12px;min-height:52px;border:0;border-radius:12px;background:#2481cc;color:#fff;font-size:17px;font-weight:600}
.fetch:disabled{opacity:.5}
.fmt{margin-top:14px;display:none}
.fmt .t{font-size:14px;color:#9aa0a6;margin-bottom:8px}
.fmt .opts{display:flex;flex-wrap:wrap;gap:8px}
.fmt button{padding:10px 14px;border:1px solid #3a3f4a;border-radius:999px;background:#0f1115;color:#e8e8e8;font-size:14px}
.fmt button.sel{background:#2481cc;border-color:#2481cc;color:#fff}
.upload{width:100%;margin-top:14px;min-height:52px;border:0;border-radius:12px;background:#2ea043;color:#fff;font-size:17px;font-weight:600;display:none}
.upload:disabled{opacity:.5}
.bar{height:10px;background:#2a2e37;border-radius:5px;margin-top:14px;overflow:hidden;display:none}
.bar i{display:block;height:100%;width:0;background:#6a9bff}
.stage{font-size:14px;color:#9aa0a6;margin-top:10px;min-height:20px}
.result{margin-top:14px;padding:12px;border-radius:12px;background:#123324;color:#7ee787;display:none}
.result.err{background:#3a1d1d;color:#ff7b7b}
.explain{margin-top:24px;font-size:14px;color:#9aa0a6}
.explain b{color:#e8e8e8}
.explain li{margin-bottom:6px}
.foot{color:#777;font-size:13px;margin-top:16px}
</style>
</head>
<body>
<div class="top"><div class="logo">&#8593;</div><div><h1>SendByLink</h1><div class="sub">Private link upload &middot; for the bot</div></div></div>
<div class="badges"><span class="perm">Permanent page</span><span>Link-only access</span><span>Not indexed by Google</span></div>
<div class="card">
<label for="url">VIDEO / FILE URL</label>
<div class="urlrow"><input id="url" type="url" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" spellcheck="false"><button id="paste" type="button">Paste</button></div>
<button id="fetch" class="fetch" type="button">Fetch</button>
<div id="fmt" class="fmt"><div class="t" id="fmttitle">Pick a quality:</div><div class="opts" id="opts"></div></div>
<button id="upload" class="upload" type="button">Upload to Owner</button>
<div id="bar" class="bar"><i id="barfill"></i></div>
<div id="stage" class="stage"></div>
<div id="result" class="result"></div>
</div>
<div class="explain">
<b>How it works:</b>
<ul>
<li>Paste a link, pick a quality, hit Upload to Owner. Nothing downloads to your phone — the bot fetches the file and uploads it straight to the owner's Telegram PM.</li>
<li>Best quality is pre-selected, so one tap on Upload is enough. Audio-only and Document are there when you want them.</li>
<li>Direct links, YouTube and 1000+ video sites, Terabox (largest file in a share wins), and custom sites all work. Images too: direct image links, Instagram posts, Reddit and Imgur pages. Files over 2 GB are refused, same as in chat.</li>
<li>This page never expires and only people with the link can open it — treat the link like a password.</li>
</ul>
</div>
<p class="foot">No cookies, nothing loaded from anywhere else, nothing logged about you. Server copy is deleted the moment the file lands on Telegram.</p>
<script>
const TOKEN = "__PAGE_TOKEN__";
const $ = id => document.getElementById(id);
const urlInput = $("url"), pasteBtn = $("paste"), fetchBtn = $("fetch");
const fmtBox = $("fmt"), optsBox = $("opts"), uploadBtn = $("upload");
const bar = $("bar"), barFill = $("barfill"), stageEl = $("stage"), resultEl = $("result");
let jobId = null, choice = "best", timer = null, formats = [];

function fmt(n){
  if (!n) return "";
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  return Math.round(n / 1024) + " KB";
}
async function api(path, opts){
  const r = await fetch("/sendlink/" + TOKEN + path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
function setStage(t){ stageEl.textContent = t; }
function showResult(text, ok){
  resultEl.style.display = "block";
  resultEl.className = "result" + (ok ? "" : " err");
  resultEl.textContent = text;
}
function reset(){
  jobId = null; choice = "best"; formats = [];
  fmtBox.style.display = "none"; uploadBtn.style.display = "none";
  bar.style.display = "none"; barFill.style.width = "0";
  resultEl.style.display = "none"; setStage("");
  fetchBtn.disabled = false; fetchBtn.textContent = "Fetch";
  urlInput.disabled = false;
}
function renderFormats(){
  optsBox.innerHTML = "";
  formats.forEach(f => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = f.label;
    if (f.id === choice) b.classList.add("sel");
    b.onclick = () => { choice = f.id; renderFormats(); };
    optsBox.appendChild(b);
  });
  fmtBox.style.display = "block";
  uploadBtn.style.display = "block";
  uploadBtn.disabled = false;
}
async function poll(){
  if (!jobId) return;
  try {
    const j = await api("/status?job=" + encodeURIComponent(jobId));
    if (j.title) $("fmttitle").textContent = "Pick a quality for: " + j.title.slice(0, 80);
    if (j.stage === "awaiting_pick" && j.formats && j.formats.length){
      formats = j.formats;
      if (!formats.some(f => f.id === choice)) choice = "best";
      renderFormats();
      setStage("Fetched — pick a quality, then Upload to Owner.");
    } else if (j.stage === "resolving"){
      setStage("Contacting the site…");
    } else if (j.stage === "downloading"){
      bar.style.display = "block";
      const pct = j.total ? Math.round(j.done / j.total * 100) : 0;
      barFill.style.width = pct + "%";
      setStage("Downloading… " + pct + "% · " + fmt(j.done) + (j.total ? " / " + fmt(j.total) : ""));
    } else if (j.stage === "uploading"){
      bar.style.display = "block"; barFill.style.width = "100%";
      setStage("Uploading to the owner's Telegram…");
    } else if (j.stage === "done"){
      clearInterval(timer); timer = null;
      bar.style.display = "block"; barFill.style.width = "100%";
      setStage("");
      const r = j.result || {};
      showResult("Sent to the owner's Telegram: " + (r.name || j.title) + (r.size ? " (" + fmt(r.size) + ")" : "") + (r.count > 1 ? " — " + r.count + " files" : ""), true);
      fetchBtn.disabled = false; fetchBtn.textContent = "Send another link";
      fetchBtn.onclick = () => { reset(); urlInput.value = ""; urlInput.focus(); };
    } else if (j.stage === "failed"){
      clearInterval(timer); timer = null;
      setStage("");
      showResult("Failed: " + (j.error || "unknown error"), false);
      fetchBtn.disabled = false; fetchBtn.textContent = "Try again";
      fetchBtn.onclick = () => reset();
    }
  } catch (e) {
    setStage("Lost contact with the server — retrying… (" + e.message + ")");
  }
}
pasteBtn.onclick = async () => {
  try {
    const t = await navigator.clipboard.readText();
    if (t) { urlInput.value = t.trim(); urlInput.focus(); return; }
  } catch (e) { /* clipboard blocked — fall through */ }
  urlInput.focus(); urlInput.select();
};
fetchBtn.onclick = async () => {
  const url = urlInput.value.trim();
  if (!url){ setStage("Paste a link first."); return; }
  fetchBtn.disabled = true; fetchBtn.textContent = "Fetching…";
  urlInput.disabled = true; resultEl.style.display = "none";
  setStage("Contacting the site…");
  try {
    const r = await api("/submit", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: url})});
    jobId = r.job;
    timer = setInterval(poll, 2000);
    poll();
  } catch (e) {
    showResult("Failed: " + e.message, false);
    fetchBtn.disabled = false; fetchBtn.textContent = "Fetch";
    urlInput.disabled = false;
  }
};
uploadBtn.onclick = async () => {
  if (!jobId) return;
  uploadBtn.disabled = true;
  setStage("Starting the upload…");
  try {
    await api("/pick", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({job: jobId, choice: choice})});
  } catch (e) {
    showResult("Failed: " + e.message, false);
    uploadBtn.disabled = false;
  }
};
</script>
</body>
</html>
"""
