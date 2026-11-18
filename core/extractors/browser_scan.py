"""Last resort after the static scrape — a real headless browser.

Some pages only reveal their video URL after JavaScript runs: the
player config is fetched, decrypted, or assembled at runtime, so no
amount of static HTML scanning finds it. Playwright drives a real
headless Chromium, loads the page, and watches what the page itself
requests — the same idea as the Ghost Downloader extension's network
sniffing, minus the extension.

Some players go one step further: they show only a thumbnail and a
play button, and request NOTHING until a visitor clicks play
(StreamFile's own code says so: "a page that is only looked at — or
loaded by a bot — adds no view"). So when the first load finds no
media, the browser clicks the play control (play buttons only — never
download/ad buttons), waits for the player to request its stream, and
checks again. Popups the click opens are closed.

Picking the right URL is the whole job: a page fires ads, previews,
posters and audio tracks as well as the real video, so candidates are
SCORED (a stream manifest beats a plain file, a video beats an audio
track, a segment URL is only a last resort) instead of taking the
first or the biggest. Extensionless links are caught too — a tokenised
stream often has no file extension and only says what it is by being a
``media`` request or by its Content-Type.

The browser session is handed back with the URL: many hosts lock the
stream to the cookies and User-Agent that loaded the page, and a
plain HTTP download without them gets a 403 for a link the browser
just played fine. So find_media() returns the cookies + headers as
well, and the callers pass them to the downloader.

What it does NOT do: pass human checks. A headless browser on a
datacenter IP still looks like a bot to Cloudflare Turnstile and
friends, so challenge pages fail fast with an honest message instead
of pretending.

Playwright is optional: the module imports fine without it
(BROWSER_AVAILABLE is False) and find_media raises BrowserError, so
hosts without the browser (Koyeb, local dev) keep working.
One browser at a time — 2 vCPU cannot run several pages well.
"""

import asyncio
import re
from urllib.parse import urlparse

from core.extractors import player_scan
from core.extractors.direct import BROWSER_UA
from core.jobs import JobCancelled

try:
    from playwright.async_api import async_playwright
    BROWSER_AVAILABLE = True
except ImportError:
    async_playwright = None  # type: ignore
    BROWSER_AVAILABLE = False

#: One browser at a time (see module docstring).
BROWSER_LOCK = asyncio.Lock()

#: Wall-clock budget for one browser visit (launch + load + scan +
#: play clicks). Raised from 75 s: the extra settle window plus up to
#: three play clicks needs the headroom, and one visit at a time is
#: still the rule.
BROWSER_TIMEOUT = 100
#: How long the first load may take before we read what it has so far.
PAGE_TIMEOUT = 20
#: After the DOM is ready, how long the page gets to go quiet. Pages
#: that poll forever never reach networkidle — the timeout is the
#: normal exit for them, not an error (and it used to be the full 20 s
#: page timeout; a short window makes slow pages much cheaper).
_SETTLE_MS = 8000

#: Play-button selectors, most specific first. ONLY these get clicked:
#: a wrong click on a download/ad button would navigate away or open
#: popups, so the list stays narrow — play controls, nothing else.
#: (StreamFile's placeholder button is first: its whole player loads
#: only after a click on it.)
_PLAY_SELECTORS = (
    "#vplayer .xvs-ph__play",
    "button[aria-label='Play']",
    "button[aria-label='play']",
    ".plyr__control--overlaid",
    "button[data-plyr='play']",
    ".vjs-big-play-button",
    ".jw-icon-display",
    "video",
    "[class*='play-button']",
    "[class*='play-btn']",
    "button[class*='play']",
)
#: Max play clicks per visit. Each waits briefly for the player to
#: request its stream; a click that produces no media still counts,
#: so a page with many play-looking buttons cannot burn the budget.
_MAX_PLAY_CLICKS = 3
#: Seconds to wait after a click for the player to request media.
#: Usually the manifest arrives in 1-2 s; the wait ends early the
#: moment a new media response arrives (see _visit's media_event).
_CLICK_WAIT = 8

_MEDIA_RE = re.compile(
    r"\.(m3u8|mpd|mp4|mkv|webm|m4v|mov|flv|ts|m4s|mp3|m4a|ogg|opus|flac|wav)"
    r"(\?|$)", re.I)
#: HLS/DASH segments — downloading one gives a few seconds of video
#: at best, so a segment only wins when the page offered nothing else.
_SEGMENT_RE = re.compile(r"\.(ts|m4s|aac|vtt)$", re.I)
#: Ad/tracker/poster URLs that are never the video, whatever their
#: extension or size says. Kept to high-confidence hosts and names.
_AD_HINTS = (
    "doubleclick", "googlesyndication", "googleadservices", "adsystem",
    "adservice", "adserver", "/ads/", "ads_banner", "preroll", "midroll",
    "/banner", "pixel.gif", "/sprite", "sprite_",
)
_CAPTCHA_MARKERS = ("cf-turnstile", "g-recaptcha", "h-captcha",
                    "challenges.cloudflare.com", "just a moment",
                    "attention required")


class BrowserError(Exception):
    pass


def _is_manifest(url: str, content_type: str = "") -> bool:
    """True for an HLS/DASH playlist (ILS manifest)."""
    ctype = (content_type or "").lower()
    if "mpegurl" in ctype or "dash+xml" in ctype:
        return True
    return urlparse(url or "").path.lower().endswith((".m3u8", ".mpd"))


def _is_media_url(url: str, content_type: str = "",
                  resource_type: str = "") -> bool:
    """A network URL (or response) that looks like media.

    Three signals, because each one alone misses real links:
      * Content-Type catches extensionless manifests (tokenised HLS
        URLs often carry no .m3u8) and plain files;
      * a ``media`` request catches what the DOM asked for even when
        the URL has no extension and the response says octet-stream;
      * the extension check catches everything else.
    Blob and data URLs are skipped: the browser made those in memory,
    so a plain HTTP download can never fetch them.
    """
    low = (url or "").lower()
    if not low or low.startswith(("blob:", "data:", "javascript:")):
        return False
    ctype = (content_type or "").lower()
    if ctype.startswith(("video/", "audio/")):
        return True
    if "mpegurl" in ctype or "dash+xml" in ctype:
        return True
    if (resource_type or "").lower() == "media":
        return True
    return bool(_MEDIA_RE.search(url or ""))


def _has_captcha(html: str, title: str) -> bool:
    low = ((html or "") + " " + (title or "")).lower()
    return any(marker in low for marker in _CAPTCHA_MARKERS)


def _score(candidate: dict) -> int | None:
    """Confidence that `candidate` is the video the page plays.

    Higher wins. None means "never this one" (an ad/tracker URL).
    The order of what matters: a stream manifest is what the player
    actually plays and segments are useless without it; then a real
    video file; then an audio track (better than nothing, and the
    "audio only" case for music pages); a bare segment last.
    """
    url = candidate.get("url") or ""
    low = url.lower()
    path = urlparse(low).path
    ctype = (candidate.get("ctype") or "").lower()
    if any(hint in low for hint in _AD_HINTS):
        return None
    if _is_manifest(url, ctype):
        score = 100
    elif ctype.startswith("video/"):
        score = 70
    elif path.endswith((".mp4", ".mkv", ".webm", ".m4v", ".mov", ".flv")):
        score = 60
    elif (candidate.get("rtype") or "").lower() == "media":
        score = 50  # extensionless, but the DOM asked for it as media
    elif ctype.startswith("audio/"):
        score = 35
    elif path.endswith((".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav")):
        score = 30
    elif _SEGMENT_RE.search(path):
        score = 5
    else:
        score = 20
    # Among equally believable URLs the bigger one is more likely the
    # feature (a 40 KB "video" is a placeholder or an ad bumper), but
    # size only nudges — it never beats a better kind of URL.
    size = candidate.get("size") or 0
    if size >= 5 * 1024 * 1024:
        score += 6
    elif size >= 512 * 1024:
        score += 3
    elif size and size < 100 * 1024:
        score -= 10
    return score


def _pick_best(candidates) -> str | None:
    """The most believable media URL from `candidates` (dicts), or None."""
    best, best_score = None, -1
    for candidate in candidates or []:
        score = _score(candidate)
        if score is None or score <= best_score:
            continue
        best, best_score = candidate, score
    return best["url"] if best else None


async def _check_media(page, network: dict, url: str):
    """Best media found so far as a dict, or None.

    Network activity first (scored — see _score), then the live DOM's
    video/audio/source elements, then the rendered HTML. Called once
    after the page loads and again after every play click — the
    player HTML only exists after the click on placeholder pages.
    """
    best = _pick_best(
        dict(url=u, ctype=c, size=s, rtype=r)
        for u, (c, s, r) in network.items())
    if best:
        ctype = (network.get(best) or ("", 0, ""))[0]
        return {"url": best, "playlist": _is_manifest(best, ctype)}
    try:
        dom_srcs = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll(
                    'video, audio, source').forEach(e => {
                        for (const a of [e.currentSrc, e.src])
                            if (a) out.push(a);
                    });
                return out;
            }""")
    except Exception:
        dom_srcs = []
    for src in dom_srcs or []:
        if _is_media_url(src):
            return {"url": src, "playlist": _is_manifest(src)}
    try:
        html = await page.content()
    except Exception:
        return None
    found = player_scan.find_media(html, url)
    if found:
        return {"url": found, "playlist": _is_manifest(found)}
    return None


async def _try_click_play(page, network: dict, url: str,
                          media_event: asyncio.Event,
                          cancel_check=None):
    """Click play controls until media appears (or attempts run out).

    Returns the media dict, or None when no click produced one. Only
    the narrow _PLAY_SELECTORS list is ever clicked; a click that
    navigates the page away is undone with go_back, and popups are
    closed by the context handler in _visit.
    """
    clicked = 0
    for selector in _PLAY_SELECTORS:
        if cancel_check is not None and cancel_check():
            raise JobCancelled()
        if clicked >= _MAX_PLAY_CLICKS:
            break
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            before = page.url
            media_event.clear()  # before the click: a fast manifest
            # arriving right after it must not be wiped by a clear
            await loc.click(timeout=5000)
        except Exception:
            continue  # not there / not clickable — try the next one
        clicked += 1
        try:
            await asyncio.wait_for(media_event.wait(), timeout=_CLICK_WAIT)
        except asyncio.TimeoutError:
            pass  # nothing new on the network — the DOM may still
            # have changed (player HTML inserted), so check anyway
        try:
            if page.url != before:
                await page.go_back()
        except Exception:
            pass
        found = await _check_media(page, network, url)
        if found:
            return found
    return None


async def _session_handoff(context, page_url: str, media_url: str):
    """Cookies + headers that let a plain HTTP download reuse the visit.

    Many hosts gate the stream on the cookies set while the player
    loaded (a session id, an anti-leech token) and on the browser's
    User-Agent. Downloading without them gets a 403 for a URL the
    browser just played. Cookies are scoped to the media URL's domain
    first; the whole context is the fallback for cross-domain CDNs
    that never see the page's cookies anyway.
    """
    headers = {"Referer": page_url, "User-Agent": BROWSER_UA}
    cookies: dict[str, str] = {}
    for scope in ([media_url], None):
        try:
            raw = await context.cookies(scope) if scope \
                else await context.cookies()
        except Exception:
            continue
        cookies = {c["name"]: c["value"] for c in raw or []
                   if c.get("name")}
        if cookies:
            break
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers, cookies


async def _visit(url: str, cancel_check=None) -> dict:
    """One browser visit: load the page, return the media dict."""
    network: dict[str, tuple[str, int, str]] = {}  # url -> (ctype, size, rtype)
    media_event = asyncio.Event()

    def _record(req_url, ctype, size, rtype):
        prev = network.get(req_url)
        if prev is None or size > prev[1]:
            network[req_url] = (ctype, size, rtype)
            media_event.set()

    def _on_response(response):
        try:
            headers = response.headers or {}
            ctype = headers.get("content-type", "")
            if _is_media_url(response.url, ctype):
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
        # A request is seen even when its response never completes
        # (a range probe, an aborted preload) and even when the URL
        # has no extension at all — the player tells us by asking
        # for it as media.
        try:
            if not _is_media_url(request.url,
                                 resource_type=request.resource_type):
                return
            if request.url in network:
                return
            _record(request.url, "", 0, request.resource_type or "")
        except Exception:
            pass

    async with async_playwright() as pw:
        # --no-sandbox: the container runs as non-root UID 1000.
        # --disable-dev-shm-usage: /dev/shm is tiny in containers and
        # Chromium crashes without this flag.
        browser = await pw.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            context = await browser.new_context(user_agent=BROWSER_UA)
            page = await context.new_page()
            page.on("response", _on_response)
            page.on("request", _on_request)

            async def _close_popup(new_page):
                # A play click can open ad popups in new tabs; they
                # are never the video, so close them.
                try:
                    if new_page is not page:
                        await new_page.close()
                except Exception:
                    pass

            context.on("page", lambda p: asyncio.create_task(_close_popup(p)))
            # A JS alert/confirm would freeze the click loop until it
            # times out; dismiss anything the page raises.
            page.on("dialog", lambda dialog: asyncio.create_task(
                _safe_dismiss(dialog)))
            try:
                # DOM first (fast), then give the page a short, bounded
                # window to go quiet — polling pages never do, and
                # waiting the full page timeout for them is wasteful.
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=PAGE_TIMEOUT * 1000)
            except Exception:
                pass  # slow/polling pages: read what loaded so far
            try:
                await page.wait_for_load_state("networkidle",
                                               timeout=_SETTLE_MS)
            except Exception:
                pass
            if cancel_check is not None and cancel_check():
                raise JobCancelled()
            html = await page.content()
            title = await page.title()
            if _has_captcha(html, title):
                raise BrowserError(
                    "that page needs a human check (captcha) before "
                    "it shows the video — download it in your browser "
                    "and send the file to the bot instead")
            found = await _check_media(page, network, url)
            if found:
                return await _finish(context, url, found)
            # Nothing yet — some players only load after a click.
            found = await _try_click_play(page, network, url, media_event,
                                          cancel_check)
            if found:
                return await _finish(context, url, found)
            # A click can also reveal a captcha ("verify to play").
            try:
                html = await page.content()
                title = await page.title()
            except Exception:
                html, title = "", ""
            if _has_captcha(html, title):
                raise BrowserError(
                    "that page needs a human check (captcha) before "
                    "it shows the video — download it in your browser "
                    "and send the file to the bot instead")
            raise BrowserError(
                "the page loaded but no video link appeared")
        finally:
            await browser.close()


async def _safe_dismiss(dialog):
    try:
        await dialog.dismiss()
    except Exception:
        pass


async def _finish(context, page_url: str, found: dict) -> dict:
    """Attach the session (cookies/headers) the download needs."""
    headers, cookies = await _session_handoff(context, page_url,
                                              found["url"])
    return {
        "url": found["url"],
        "playlist": bool(found.get("playlist")),
        "headers": headers,
        "cookies": cookies,
        "page_url": page_url,
    }


async def find_media(url: str, cancel_check=None) -> dict:
    """Render `url` in a headless browser; return what to download.

    The dict carries:
      url       — the media URL the player used,
      playlist  — True for an HLS/DASH manifest (hand it to yt-dlp),
      headers   — Referer + User-Agent + Cookie for the download,
      cookies   — the same cookies as a name/value dict,
      page_url  — the page the URL was found on.

    Raises BrowserError when the browser is not installed, the page
    needs a captcha, the visit times out, or no media appears.
    `cancel_check`, when given, is polled at stage boundaries — a
    True answer raises JobCancelled (the browser is closed first).
    """
    if not BROWSER_AVAILABLE:
        raise BrowserError(
            "the browser scraper isn't installed in this build")
    if cancel_check is not None and cancel_check():
        raise JobCancelled()
    async with BROWSER_LOCK:
        try:
            return await asyncio.wait_for(_visit(url, cancel_check),
                                          timeout=BROWSER_TIMEOUT)
        except asyncio.TimeoutError:
            raise BrowserError(
                "the page took too long to load in the browser")


async def find_media_url(url: str) -> str:
    """Just the media URL (callers that don't need the session)."""
    return (await find_media(url))["url"]
