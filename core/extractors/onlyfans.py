"""Custom extractor: OnlyFans posts, with the owner's cookies only.

Why this exists: no engine understands OnlyFans — yt-dlp and
gallery-dl have no OnlyFans extractor (verified 2026-11-28) — and
the OF-Scraper route (v1.51.18, reverted before deploy) needed four
values copied out of DevTools (sess, auth_id, x-bc, user_agent).
This one needs nothing but the login the owner already exports:
a cookies.txt installed with /setcookies (auto-detect files it as
the onlyfans.com jar). The headless browser loads the post page
logged in — the page's own JavaScript does the API signing, so the
bot never has to — and the media URLs the page fetches are
downloaded with that same session.

How it works: Playwright (the same Chromium the browser scanner
uses) opens the post with the jar's cookies, waits for the post
media to load, and collects every image/video the page actually
fetched (network log first, DOM as fallback). Files go through the
bot's normal downloaders: direct files via download_file, HLS/DASH
manifests via yt-dlp. A post's media are the big fetches — avatars
and icons are filtered out by size.

Limits, stated plainly: a page that answers with a human check
(Cloudflare) fails fast with that message instead of pretending
(/browser is the fallback then). DRM-protected videos cannot be
captured — the browser plays them encrypted, and the segments stay
encrypted without a CDM, so those fail with a plain message. Only
single post links (onlyfans.com/<id>/<user>); profile pages are
refused — the bot is a paste-one-link tool, not a timeline scraper.

Verified 2026-11-28: URL matching + cookie gate, the Netscape ->
Playwright cookie parse, the media filter, and a full browser run
against a local test page (fake post, real Chromium) — test_v1529.
NOT verified live: a real OnlyFans post — that needs the owner's
real cookies and the Space's IP, and happens after deploy.
"""

import asyncio
import os
import re
from urllib.parse import urlparse

from core.cookies import jar_for_url
from core.extractors import browser_scan
from core.extractors.browser_scan import (
    BROWSER_LOCK, BROWSER_TIMEOUT, BROWSER_UA, PAGE_TIMEOUT,
    _SETTLE_MS, _has_captcha, _is_manifest, _is_media_url,
    _session_handoff, async_playwright)
from core.extractors.direct import download_file
from core.extractors.ytdlp_engine import download as ytdlp_download
from core.jobs import JobCancelled
from core.utils import list_files

#: Hosts that serve OnlyFans post pages.
HOSTS = ("onlyfans.com", "www.onlyfans.com")

#: OnlyFans post: /<digits>/<username> (or just /<digits>).
_POST_RE = re.compile(r"^/(\d+)(?:/([^/]+))?/?$")

#: A fetched media file smaller than this is an avatar, icon or
#: emoji — not post media. Post photos/videos are far larger.
_MIN_MEDIA_BYTES = 30 * 1024

#: browser_scan._is_media_url is video-focused (the scanner looks
#: for videos) — it does not match image content-types or photo
#: extensions, and an OnlyFans post is often photos. This is the
#: same three-signal idea, extended to images.
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif",
                ".bmp", ".tiff")


def _is_of_media_url(url: str, content_type: str = "",
                     resource_type: str = "") -> bool:
    low = (url or "").lower()
    if not low or low.startswith(("blob:", "data:", "javascript:")):
        return False
    if _is_media_url(url, content_type, resource_type):
        return True
    if (content_type or "").lower().startswith("image/"):
        return True
    if (resource_type or "").lower() == "image":
        return True
    return urlparse(low).path.endswith(_IMAGE_EXTS)

#: Scroll steps to trigger lazy-loaded post media.
_SCROLLS = 4


class OnlyFansError(Exception):
    pass


def _site_url(url: str) -> str | None:
    """The post URL, or None when it is not an OnlyFans post link."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return None
    if (parsed.netloc or "").lower() not in HOSTS:
        return None
    if not _POST_RE.match(parsed.path or ""):
        return None
    return url


def is_site_url(url: str) -> bool:
    """True only for a post link when the OnlyFans cookies are
    installed. Without the jar this never matches, so the chain is
    untouched until the owner installs the cookies."""
    if _site_url(url) is None:
        return False
    return jar_for_url(url) is not None


def _title(url: str) -> str:
    match = _POST_RE.match(urlparse(url).path or "")
    post_id = match.group(1) if match else ""
    if match and match.group(2):
        return f"OnlyFans post {post_id} by {match.group(2)}"
    return f"OnlyFans post {post_id}" if post_id else "OnlyFans post"


async def resolve(url: str) -> dict:
    """Cheap info for the size check + caption. No network: the real
    work happens in download()."""
    return {
        "title": _title(url),
        "media_url": "",
        "approx_size": 0,
        "is_playlist": False,
    }


def _parse_jar(path: str) -> list[dict]:
    """Netscape cookies.txt -> Playwright cookie dicts, OnlyFans
    domains only. HttpOnly lines carry a '#HttpOnly_' prefix (see
    cookies._cookie_lines); the domain field may start with '.'."""
    cookies: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
                http_only = True
            else:
                http_only = False
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, cpath, secure, expires, name, value = parts[:7]
            domain = domain.lstrip(".").lower()
            if domain != "onlyfans.com" and \
                    not domain.endswith(".onlyfans.com"):
                continue
            try:
                exp = int(float(expires))
            except ValueError:
                exp = -1
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": cpath or "/",
                "expires": exp if exp > 0 else -1,
                "httpOnly": http_only,
                "secure": secure.upper() == "TRUE",
                "sameSite": "Lax",
            })
    return cookies


def _is_post_media(url: str, ctype: str, size: int) -> bool:
    """A fetched URL that is post media (not chrome around it)."""
    if not _is_of_media_url(url, ctype):
        return False
    # Images/videos the page fetched for the post are large; the
    # avatar, header and icons are small. Size 0 means the size is
    # unknown (a request seen without its response) — keep those;
    # the DOM fallback never carries a size either.
    if size and size < _MIN_MEDIA_BYTES:
        return False
    return True


async def _collect(page, network: dict) -> list[dict]:
    """All post-media URLs the page fetched, biggest first.

    Network log first (it has content-type + size, so avatars can
    be filtered); the live DOM only fills gaps — a video element
    whose request was recorded without a response, or an image the
    page has in the DOM but hasn't fetched yet.
    """
    found: dict[str, dict] = {}
    for media_url, (ctype, size, _rtype) in network.items():
        if _is_post_media(media_url, ctype, size):
            found[media_url] = {
                "url": media_url,
                "playlist": _is_manifest(media_url, ctype),
                "size": size,
            }
    if not found:
        try:
            dom_srcs = await page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll(
                        'video, audio, source, img').forEach(e => {
                        for (const a of [e.currentSrc, e.src,
                                        e.poster])
                            if (a) out.push(a);
                        (e.srcset || '').split(',').forEach(
                            s => { const u = s.trim().split(' ')[0];
                                   if (u) out.push(u); });
                    });
                    return out;
                }""")
        except Exception:
            dom_srcs = []
        for src in dom_srcs or []:
            if src not in found and _is_of_media_url(src):
                found[src] = {"url": src, "playlist": _is_manifest(src),
                              "size": 0}
    return sorted(found.values(), key=lambda c: -c["size"])


async def _visit(url: str, cookies: list[dict],
                 cancel_check=None) -> dict:
    """One browser visit with the owner's cookies. Returns the media
    list + the session (headers/cookies) the download needs."""
    if not browser_scan.BROWSER_AVAILABLE or async_playwright is None:
        raise OnlyFansError(
            "the browser scraper isn't installed in this build")
    network: dict[str, tuple[str, int, str]] = {}

    def _record(req_url, ctype, size, rtype):
        prev = network.get(req_url)
        if prev is None or size > prev[1]:
            network[req_url] = (ctype, size, rtype)

    def _on_response(response):
        try:
            headers = response.headers or {}
            ctype = headers.get("content-type", "")
            if _is_of_media_url(response.url, ctype):
                size = 0
                try:
                    size = int(headers.get("content-length") or 0)
                except ValueError:
                    pass
                rtype = ""
                try:
                    rtype = response.request.resource_type
                except Exception:
                    pass
                _record(response.url, ctype, size, rtype)
        except Exception:
            pass

    def _on_request(request):
        try:
            if not _is_of_media_url(
                    request.url,
                    resource_type=request.resource_type):
                return
            if request.url in network:
                return
            _record(request.url, "", 0, request.resource_type or "")
        except Exception:
            pass

    async with BROWSER_LOCK:
        try:
            return await asyncio.wait_for(
                _visit_inner(url, cookies, network, _on_response,
                             _on_request, cancel_check),
                timeout=BROWSER_TIMEOUT)
        except asyncio.TimeoutError:
            raise OnlyFansError(
                "the OnlyFans page took too long to load in the "
                "browser")


async def _visit_inner(url, cookies, network, _on_response, _on_request,
                       cancel_check):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            context = await browser.new_context(user_agent=BROWSER_UA)
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            page.on("response", _on_response)
            page.on("request", _on_request)
            page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=PAGE_TIMEOUT * 1000)
            except Exception:
                pass  # slow page: read what loaded so far
            try:
                await page.wait_for_load_state("networkidle",
                                               timeout=_SETTLE_MS)
            except Exception:
                pass
            # Lazy-loaded post media only fetches once in view.
            for _ in range(_SCROLLS):
                try:
                    await page.evaluate(
                        "window.scrollBy(0, window.innerHeight)")
                    await page.wait_for_timeout(600)
                except Exception:
                    break
            try:
                await page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            if cancel_check is not None and cancel_check():
                raise JobCancelled()
            html = await page.content()
            title = await page.title()
            if _has_captcha(html, title):
                raise OnlyFansError(
                    "OnlyFans showed a human check to the bot's "
                    "browser — download that post in your own browser "
                    "and send the file to the bot instead")
            media = await _collect(page, network)
            if not media:
                raise OnlyFansError(
                    "the post page loaded but no photo or video "
                    "appeared — the cookies may have expired (log in "
                    "and re-export them), or the post may be gone")
            headers, session_cookies = await _session_handoff(
                context, url, media[0]["url"])
            return {"media": media, "headers": headers,
                    "cookies": session_cookies, "page_url": url}
        finally:
            await browser.close()


async def download(url: str, job_dir: str, progress_cb=None,
                   cancel_check=None) -> list[str]:
    if _site_url(url) is None:
        raise OnlyFansError("not an OnlyFans post link")
    jar = jar_for_url(url)
    if jar is None:
        raise OnlyFansError(
            "no OnlyFans cookies installed — export a cookies.txt "
            "while logged into onlyfans.com and send it with "
            "/setcookies (auto-detect)")
    cookies = _parse_jar(jar)
    if not cookies:
        raise OnlyFansError(
            "the OnlyFans cookies file has no onlyfans.com cookies "
            "in it — export it while logged in and try again")
    if not any(c["name"] in ("sess", "auth_id") for c in cookies):
        raise OnlyFansError(
            "the cookies file has no logged-in session (no sess / "
            "auth_id) — export it while logged into onlyfans.com")

    visit = await _visit(url, cookies, cancel_check)
    headers = visit["headers"]
    session_cookies = visit["cookies"]
    before = set(list_files(job_dir))
    errors: list[str] = []

    async def _progress(done, total):
        if progress_cb is not None:
            await progress_cb(done, total)

    for item in visit["media"]:
        if cancel_check is not None and cancel_check():
            raise JobCancelled()
        media_url = item["url"]
        try:
            if item["playlist"]:
                # HLS/DASH: yt-dlp fetches the segments.
                await ytdlp_download(
                    media_url, job_dir, progress_cb=_progress,
                    headers=headers, cancel_check=cancel_check)
            else:
                await download_file(
                    media_url, job_dir, progress_cb=_progress,
                    headers=headers, cookies=session_cookies)
        except Exception as exc:  # noqa: BLE001 — one bad file
            # must not kill the rest of the post's media.
            errors.append(f"{media_url[:80]}: {exc}")

    new_files = [p for p in list_files(job_dir) if p not in before]
    if new_files:
        # The inner downloaders (download_file / yt-dlp) already
        # recorded these paths in stats; recording again would
        # double-count the bytes.
        return new_files
    hint = errors[-1][:300] if errors else "nothing downloaded"
    raise OnlyFansError(
        "OnlyFans showed media but none of it downloaded — "
        f"{hint}")
