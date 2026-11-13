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

Flow (one tap, the way the owner actually uses it — he grabs
direct links from the browser and does not want to switch to
Telegram each time): paste link -> Fetch -> the job card drops
below and works through Probe -> Check -> Download -> Upload,
then lands in the owner's bot PM. No quality picker, no second
button: best quality, always. The textbox clears the moment a
job is accepted, so the next link can be pasted straight away.
Download and upload show live speed (average since the step
started) and time left; the done card shows how long the job
took. Upload progress comes from sender.send_files' cumulative
progress callback — media groups have no live hook, so photo
sets only report each finished group.
At most 5 jobs at once per page link (2 really downloading —
the bot's JOB_SEMAPHORE — the rest queue); a 6th submit is
refused plainly until one finishes. Job cards are the history:
they live 1 hour, then the record is deleted (the file itself
is deleted much sooner — the moment its upload finishes).
The page polls the job list, so it never needs a refresh.

Terabox shares with several files take the largest file.
Zips are sent as-is (the owner can unzip them with the bot
later).

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
    JOB_SEMAPHORE, SizeBlocked, check_size, cleanup_dir,
    make_job_dir)

log = logging.getLogger("all4url.sendlink")

#: Job records (the on-page history) live 1 hour, then the record
#: is deleted. The downloaded file itself is deleted much sooner:
#: the moment its Telegram upload finishes, success or fail.
JOB_TTL = 3600
#: At most this many jobs at once per page link. Two really
#: download at a time (the bot's JOB_SEMAPHORE); the rest queue.
#: A submit past this is refused plainly until one finishes.
MAX_ACTIVE_JOBS = 5
#: Stages a job walks through, in order.
ACTIVE_STAGES = ("probing", "checking", "queued", "downloading",
                 "uploading")

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


def _new_job(url: str, uid: int | None, token_r: str | None) -> dict:
    job_id = secrets.token_hex(8)
    _JOBS[job_id] = {
        "id": job_id, "url": url, "uid": uid, "token_r": token_r,
        "stage": "probing", "title": "", "done": 0, "total": 0,
        "up_done": 0, "up_total": 0,
        "dl_started": None, "up_started": None, "finished": None,
        "result": None, "error": None, "failed_at": None,
        "created": int(time.time()),
        "resolved": None,
    }
    return _JOBS[job_id]


def _prune() -> None:
    now = int(time.time())
    for job_id, job in list(_JOBS.items()):
        if job["created"] + JOB_TTL <= now:
            _JOBS.pop(job_id, None)


def _speed(done: int, started) -> float | None:
    """Average bytes/second since a stage started — None until there
    is at least a second of data, so the card never shows a wild
    number from the first half-second."""
    if not started or not done:
        return None
    elapsed = time.time() - started
    if elapsed < 1:
        return None
    return done / elapsed


def _eta(done: int, total: int, speed) -> float | None:
    """Seconds left at the current average speed — None when the
    total size is unknown or the job is already done."""
    if not speed or not total or done >= total:
        return None
    return (total - done) / speed


def _public(job: dict) -> dict:
    """What the page is allowed to see — no internal state, no uid.
    The url is shown back to the same link-holder who pasted it.
    Speeds are averages since each stage started; ETAs are the
    remaining bytes at that average speed."""
    dl_speed = _speed(job.get("done") or 0, job.get("dl_started"))
    up_speed = _speed(job.get("up_done") or 0, job.get("up_started"))
    duration = None
    if job.get("finished") and job.get("created"):
        duration = int(job["finished"] - job["created"])
    return {"id": job["id"], "url": job.get("url") or "",
            "stage": job["stage"], "title": job.get("title") or "",
            "done": job.get("done") or 0, "total": job.get("total") or 0,
            "up_done": job.get("up_done") or 0,
            "up_total": job.get("up_total") or 0,
            "dl_speed": dl_speed,
            "dl_eta": _eta(job.get("done") or 0, job.get("total") or 0,
                           dl_speed),
            "up_speed": up_speed,
            "up_eta": _eta(job.get("up_done") or 0,
                           job.get("up_total") or 0, up_speed),
            "up_elapsed": (int(time.time() - job["up_started"])
                           if job.get("up_started")
                           and job["stage"] == "uploading" else None),
            "duration": duration,
            "result": job.get("result"),
            "error": job.get("error"), "failed_at": job.get("failed_at"),
            "created": job.get("created") or 0}


def _active_count(token_r: str | None) -> int:
    return sum(1 for job in _JOBS.values()
               if job.get("token_r") == token_r
               and job["stage"] in ACTIVE_STAGES)


async def _probe(url: str) -> dict:
    """Work out what a link is WITHOUT downloading it yet. Returns
    {kind, title, size, formats, ...} for the download step.
    It does NOT enforce any size cap — the Check step does that,
    so the page can honestly show Probe, then Check, as two
    separate steps. Order mirrors the bot's resolver chain in
    plugins/uploader.py."""
    errors: list[str] = []
    # 1) Terabox — largest file wins (no picker on a web page).
    if terabox.is_terabox_url(url):
        try:
            files = await terabox.list_files(url)
            if not files:
                raise RuntimeError("that Terabox share looks empty")
            biggest = max(files, key=lambda f: f.get("size") or 0)
            return {"kind": "terabox", "url": url,
                    "title": biggest.get("name") or "file",
                    "size": biggest.get("size") or 0,
                    "index": files.index(biggest),
                    "via": biggest.get("via")}
        except RuntimeError:
            raise
        except Exception as exc:
            errors.append(f"terabox: {exc}")
    # 2) Custom-site extractors (luluvido etc.).
    site = site_registry.match(url)
    if site:
        try:
            info = await site.resolve(url)
            return {"kind": "custom", "url": url, "site": site,
                    "title": info.get("title") or "file",
                    "size": info.get("approx_size") or 0}
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
        return {"kind": "direct", "url": url,
                "final_url": direct_info.get("final_url") or url,
                "title": direct_info.get("filename") or "file",
                "size": direct_info.get("size") or 0}
    # 4) yt-dlp — the 1000+ sites it understands.
    try:
        info = await extract_info(url)
        title = info.get("title") or "Download"
        formats = best_formats(info) if info.get("formats") else []
        size = formats[0].get("filesize") or 0 if formats else 0
        return {"kind": "ytdlp", "url": url, "title": title,
                "size": size, "formats": formats}
    except Exception as exc:
        errors.append(f"yt-dlp: {exc}")
    # 5) gallery-dl covers image hosts and galleries yt-dlp does
    # not (Reddit posts, Imgur albums, image boards…). It only
    # proves itself by downloading, so resolve hands the job to it
    # and the download step tries it — with the page-scrape as its
    # own fallback, exactly like the bot's chain.
    return {"kind": "gallery", "url": url, "title": url, "size": 0,
            "errors": errors}


async def _download_resolved(resolved: dict, job_dir: str,
                             progress) -> list[str]:
    """Download the probed link — best quality, always. There is
    no format choice on this page by design (one tap)."""
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
        return await ytdlp_download(url, job_dir, progress_cb=progress)
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
                         uid: int | None, progress_cb=None) -> None:
    """Deliver finished files to the owner's bot PM, bypassing
    /destination — same rule as upage admin delivery. `progress_cb`
    gets (sent_so_far, total) across all files, for the page's
    upload speed + ETA."""
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
    from core.sender import send_files
    sent = await send_files(_bot, config.OWNER_ID, paths,
                            caption=title[:200], progress_cb=progress_cb)
    if not sent:
        raise RuntimeError("Telegram returned nothing for the file")


def _fail(job: dict, exc: Exception) -> None:
    """Mark a job failed, remembering WHICH step it died on — the
    page tells them apart: a link that could not be probed or
    downloaded is a link failure, a failure while sending is a
    Telegram upload failure."""
    job["stage"] = "failed"
    job["failed_at"] = job.get("stage") or "probing"
    job["error"] = str(exc)[:500]
    stats.record_job("sendlink", ok=False)


async def _run_job(job_id: str) -> None:
    """Background job: probe -> check -> (queue) -> download ->
    upload -> delete the server copy. One tap starts it; the page
    polls the job list, so this task never holds an HTTP request
    open and the textbox is free for the next link at once."""
    job = _JOBS.get(job_id)
    if job is None:
        return
    url, uid = job["url"], job["uid"]
    try:
        # 1) Probe — what is this link, and how big is it?
        job["stage"] = "probing"
        resolved = await _probe(url)
        job["resolved"] = resolved
        job["title"] = resolved.get("title") or "Download"
        job["total"] = resolved.get("size") or 0
        # 2) Check — monthly bandwidth left? File within the
        # Telegram/disk limits? Refusing here costs nothing yet.
        job["stage"] = "checking"
        if stats.bandwidth_blocked():
            raise RuntimeError(stats.bandwidth_pause_text())
        check_size(resolved.get("size") or 0, job["title"])

        async def progress(done, total):
            job["done"] = done
            job["total"] = total or job["total"]

        # 3) Queue, then download — at most 2 really download at
        # once (JOB_SEMAPHORE, shared with the bot's own jobs).
        job["stage"] = "queued"
        await JOB_SEMAPHORE.acquire()
        try:
            job["stage"] = "downloading"
            job["dl_started"] = time.time()
            job_dir = make_job_dir()
            try:
                paths = await _download_resolved(resolved, job_dir,
                                                 progress)
                # 4) Upload to the owner's bot PM, then the server
                # copy is deleted (cleanup_dir, in the finally).
                job["stage"] = "uploading"
                job["done"] = job["total"]
                job["up_started"] = time.time()
                job["up_total"] = sum(
                    os.path.getsize(p) for p in paths
                    if os.path.exists(p))

                async def up_progress(done, total):
                    job["up_done"] = done
                    job["up_total"] = total or job["up_total"]

                await _send_to_owner(paths, job["title"], uid,
                                     progress_cb=up_progress)
                total_bytes = sum(
                    os.path.getsize(p) for p in paths
                    if os.path.exists(p))
                job["result"] = {
                    "name": os.path.basename(paths[0]) if paths else job["title"],
                    "size": total_bytes,
                    "count": len(paths)}
                job["stage"] = "done"
                job["finished"] = time.time()
                stats.record_job("sendlink", ok=True)
            finally:
                cleanup_dir(job_dir)
        finally:
            JOB_SEMAPHORE.release()
    except (SizeBlocked, RuntimeError, YtdlpError) as exc:
        _fail(job, exc)
    except Exception as exc:
        log.warning("sendlink job %s failed: %s", job_id, exc)
        _fail(job, exc)


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
    # Too many at once? The page lets the owner paste the next
    # link immediately, so the cap has to live here: at most
    # MAX_ACTIVE_JOBS per page link, then a plain refusal.
    token_r = payload.get("r")
    if _active_count(token_r) >= MAX_ACTIVE_JOBS:
        return web.json_response(
            {"error": f"too many jobs running already "
                      f"(max {MAX_ACTIVE_JOBS} at once) — wait for "
                      f"one to finish, then paste again"},
            status=429)
    job = _new_job(url, payload.get("uid"), token_r)
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


async def jobs(request: web.Request) -> web.Response:
    """The page's job history: every job this page link started,
    newest first, each with its stage and error. Records older
    than JOB_TTL (1 hour) are pruned away — the history deletes
    itself, while the files were already deleted at upload time.
    Jobs are scoped to this link's token, so one link-holder
    never sees another link's jobs."""
    payload = await _require_page(request)
    _prune()
    token_r = payload.get("r")
    mine = [job for job in _JOBS.values()
            if job.get("token_r") == token_r]
    mine.sort(key=lambda job: job.get("created") or 0, reverse=True)
    return web.json_response({"jobs": [_public(job) for job in mine]})


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
    app.router.add_get("/sendlink/{token}/jobs", jobs)


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
.top{display:flex;align-items:center;gap:12px;margin-bottom:12px}
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
.urlrow button{height:52px;padding:0 14px;border:1px solid #3a3f4a;border-radius:12px;background:#2a2e37;color:#e8e8e8;font-size:15px}
.urlrow button.clear{padding:0 12px;color:#9aa0a6}
.fetch{width:100%;margin-top:12px;min-height:52px;border:0;border-radius:12px;background:#2481cc;color:#fff;font-size:17px;font-weight:600}
.fetch:disabled{opacity:.5}
.formmsg{font-size:14px;color:#ff7b7b;margin-top:10px;min-height:20px}
.job{background:#1b1e24;border-radius:16px;padding:14px 16px;margin-top:12px}
.job .title{font-size:15px;font-weight:600;word-break:break-all}
.job .url{font-size:13px;color:#9aa0a6;word-break:break-all}
.job .time{font-size:12px;color:#777;margin-top:2px}
.steps{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.steps span{font-size:13px;padding:5px 10px;border-radius:999px;background:#2a2e37;color:#9aa0a6}
.steps span.done{background:#123324;color:#7ee787}
.steps span.active{background:#123a5c;color:#7db8ff}
.steps span.failed{background:#3a1d1d;color:#ff7b7b}
.steps span.wait{background:#2a2e37;color:#e8b93e}
.bar{height:10px;background:#2a2e37;border-radius:5px;margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:#6a9bff}
.job .status{font-size:14px;margin-top:8px}
.job .status.ok{color:#7ee787}
.job .status.err{color:#ff7b7b}
.job .status.muted{color:#9aa0a6}
.empty{color:#777;font-size:14px;margin-top:16px;text-align:center}
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
<div class="urlrow"><input id="url" type="url" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" spellcheck="false"><button id="clear" type="button" class="clear" title="Clear">&#10005;</button><button id="paste" type="button">Paste</button></div>
<button id="fetch" class="fetch" type="button">Fetch</button>
<div id="formmsg" class="formmsg"></div>
</div>
<div id="jobs"></div>
<div id="empty" class="empty">No links yet — paste one above and hit Fetch.</div>
<div class="explain">
<b>How it works:</b>
<ul>
<li>Paste a link and hit Fetch — that's the only tap. The job card below walks through Probe, Check, Download, Upload, and the file lands in the owner's Telegram PM. Best quality, always. Download and upload show live speed and time left.</li>
<li>The box clears itself after each Fetch, so paste the next link straight away. Up to 5 jobs at once (2 downloading, the rest queue).</li>
<li>Direct links, YouTube and 1000+ video sites, Terabox (largest file in a share wins), custom sites, and images too: direct image links, Instagram posts, Reddit and Imgur pages. Files over 2 GB are refused, same as in chat.</li>
<li>Job history here deletes itself after 1 hour. The downloaded file itself is deleted much sooner — the moment it lands on Telegram.</li>
<li>This page never expires and only people with the link can open it — treat the link like a password.</li>
</ul>
</div>
<p class="foot">No cookies, nothing loaded from anywhere else, nothing logged about you.</p>
<script>
const TOKEN = "__PAGE_TOKEN__";
const $ = id => document.getElementById(id);
const urlInput = $("url"), pasteBtn = $("paste"), clearBtn = $("clear"), fetchBtn = $("fetch");
const formMsg = $("formmsg"), jobsEl = $("jobs"), emptyEl = $("empty");
const STEPS = ["probing", "checking", "downloading", "uploading"];
const LABELS = {probing: "Probe", checking: "Check", downloading: "Download", uploading: "Upload"};

function fmt(n){
  if (!n) return "";
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  return Math.round(n / 1024) + " KB";
}
function fmtSpeed(bps){
  if (!bps) return "";
  if (bps >= 1048576) return (bps / 1048576).toFixed(1) + " MB/s";
  if (bps >= 1024) return Math.round(bps / 1024) + " KB/s";
  return Math.round(bps) + " B/s";
}
function fmtEta(sec){
  if (sec === null || sec === undefined) return "";
  sec = Math.round(sec);
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m " + String(sec % 60).padStart(2, "0") + "s";
  return Math.floor(sec / 3600) + "h " + String(Math.floor(sec / 60) % 60).padStart(2, "0") + "m";
}
function ago(ts){
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000) - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  return Math.floor(s / 3600) + " h ago";
}
async function api(path, opts){
  const r = await fetch("/sendlink/" + TOKEN + path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function stepState(job, step){
  const order = {probing: 0, checking: 1, queued: 2, downloading: 2, uploading: 3, done: 4, failed: -1};
  const cur = order[job.stage] === undefined ? 0 : order[job.stage];
  const idx = STEPS.indexOf(step);
  if (job.stage === "done") return "done";
  if (job.stage === "failed"){
    const failedIdx = order[job.failed_at] === undefined ? -1 : order[job.failed_at];
    if (idx === failedIdx) return "failed";
    return idx < failedIdx ? "done" : "";
  }
  if (job.stage === "queued"){
    if (idx < 2) return "done";
    if (idx === 2) return "wait";
    return "";
  }
  if (idx < cur) return "done";
  if (idx === cur) return "active";
  return "";
}
function renderJobs(list){
  jobsEl.innerHTML = "";
  emptyEl.style.display = list.length ? "none" : "block";
  list.forEach(job => {
    const card = document.createElement("div");
    card.className = "job";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = job.title || job.url;
    card.appendChild(title);
    if (job.title && job.url){
      const u = document.createElement("div");
      u.className = "url";
      u.textContent = job.url.length > 100 ? job.url.slice(0, 100) + "…" : job.url;
      card.appendChild(u);
    }
    const t = document.createElement("div");
    t.className = "time";
    t.textContent = ago(job.created);
    card.appendChild(t);
    const steps = document.createElement("div");
    steps.className = "steps";
    STEPS.forEach(step => {
      const chip = document.createElement("span");
      const st = stepState(job, step);
      if (st) chip.className = st;
      chip.textContent = (st === "done" ? "✓ " : st === "failed" ? "✗ " : "") + LABELS[step] + (st === "wait" ? " — waiting…" : st === "active" ? "…" : "");
      steps.appendChild(chip);
    });
    card.appendChild(steps);
    const barStage = job.stage === "downloading" ? {done: job.done, total: job.total}
      : job.stage === "uploading" ? {done: job.up_done, total: job.up_total} : null;
    if (barStage && barStage.total){
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("i");
      fill.style.width = Math.round(barStage.done / barStage.total * 100) + "%";
      bar.appendChild(fill);
      card.appendChild(bar);
    }
    const status = document.createElement("div");
    status.className = "status";
    if (job.stage === "done"){
      status.classList.add("ok");
      const r = job.result || {};
      status.textContent = "Sent to the owner's Telegram: " + (r.name || job.title) + (r.size ? " (" + fmt(r.size) + ")" : "") + (r.count > 1 ? " — " + r.count + " files" : "") + (job.duration ? " · took " + fmtEta(job.duration) : "");
    } else if (job.stage === "failed"){
      status.classList.add("err");
      const where = job.failed_at === "uploading" ? "Telegram upload failed" : "Link failed";
      status.textContent = where + ": " + (job.error || "unknown error");
    } else if (job.stage === "queued"){
      status.classList.add("muted");
      status.textContent = "Waiting for a free slot…";
    } else if (job.stage === "downloading"){
      status.classList.add("muted");
      let t = "Downloading… " + (job.total ? Math.round(job.done / job.total * 100) + "% · " + fmt(job.done) + " / " + fmt(job.total) : fmt(job.done));
      if (job.dl_speed) t += " · " + fmtSpeed(job.dl_speed);
      if (job.dl_eta !== null && job.dl_eta !== undefined) t += " · ~" + fmtEta(job.dl_eta) + " left";
      status.textContent = t;
    } else if (job.stage === "uploading"){
      status.classList.add("muted");
      let t = "Uploading to the owner's Telegram…";
      if (job.up_total){
        t = "Uploading… " + Math.round(job.up_done / job.up_total * 100) + "% · " + fmt(job.up_done) + " / " + fmt(job.up_total);
        if (job.up_speed) t += " · " + fmtSpeed(job.up_speed);
        if (job.up_eta !== null && job.up_eta !== undefined) t += " · ~" + fmtEta(job.up_eta) + " left";
      } else if (job.up_elapsed){
        t += " " + fmtEta(job.up_elapsed);
      }
      status.textContent = t;
    } else if (job.stage === "checking"){
      status.classList.add("muted");
      status.textContent = "Checking size and bandwidth…";
    } else {
      status.classList.add("muted");
      status.textContent = "Probing the link…";
    }
    card.appendChild(status);
    jobsEl.appendChild(card);
  });
}
async function refresh(){
  try {
    const r = await api("/jobs");
    renderJobs(r.jobs || []);
  } catch (e) {
    /* keep the old list on a blip; next poll retries */
  }
}
pasteBtn.onclick = async () => {
  try {
    const t = await navigator.clipboard.readText();
    if (t) { urlInput.value = t.trim(); urlInput.focus(); return; }
  } catch (e) { /* clipboard blocked — fall through */ }
  urlInput.focus(); urlInput.select();
};
clearBtn.onclick = () => { urlInput.value = ""; formMsg.textContent = ""; urlInput.focus(); };
fetchBtn.onclick = async () => {
  const url = urlInput.value.trim();
  if (!url){ formMsg.textContent = "Paste a link first."; return; }
  fetchBtn.disabled = true; fetchBtn.textContent = "Fetching…";
  formMsg.textContent = "";
  try {
    await api("/submit", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: url})});
    urlInput.value = "";
    urlInput.focus();
    refresh();
  } catch (e) {
    formMsg.textContent = e.message;
  } finally {
    fetchBtn.disabled = false; fetchBtn.textContent = "Fetch";
  }
};
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""