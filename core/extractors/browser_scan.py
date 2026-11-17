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
media, the browser clicks the play control (play buttons only —
never download/ad buttons), waits for the player to request its
stream, and checks again. Popups the click opens are closed.

What it does NOT do: pass human checks. A headless browser on a
datacenter IP still looks like a bot to Cloudflare Turnstile and
friends, so challenge pages fail fast with an honest message
instead of pretending.

Playwright is optional: the module imports fine without it
(BROWSER_AVAILABLE is False) and find_media_url raises BrowserError,
so hosts without the browser (Koyeb, local dev) keep working.
One browser at a time — 2 vCPU cannot run several pages well.
"""

import asyncio
import re

from core.extractors import player_scan
from core.extractors.direct import BROWSER_UA

try:
    from playwright.async_api import async_playwright
    BROWSER_AVAILABLE = True
except ImportError:
    async_playwright = None  # type: ignore
    BROWSER_AVAILABLE = False

#: One browser at a time (see module docstring).
BROWSER_LOCK = asyncio.Lock()

#: Wall-clock budget for one browser visit (launch + load + scan).
BROWSER_TIMEOUT = 75
#: How long a page gets to settle before we read what it has so far.
#: Pages that poll forever never reach networkidle — the timeout is
#: the normal exit for them, not an error.
PAGE_TIMEOUT = 20

#: Play-button selectors, most specific first. ONLY these get clicked:
#: a wrong click on a download/ad button would navigate away or open
#: popups, so the list stays narrow — play controls, nothing else.
#: (StreamFile's placeholder button is first: its whole player loads
#: only after a click on it.)
_PLAY_SELECTORS = (
    "#vplayer .xvs-ph__play",
    "button[aria-label='Play']",
    ".plyr__control--overlaid",
    "button[data-plyr='play']",
    ".vjs-big-play-button",
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
    r"\.(m3u8|mpd|mp4|mkv|webm|m4v|mov|ts|m4s|mp3|m4a|ogg|opus|flac|wav)"
    r"(\?|$)", re.I)
_CAPTCHA_MARKERS = ("cf-turnstile", "g-recaptcha", "h-captcha",
                    "challenges.cloudflare.com", "just a moment",
                    "attention required")


class BrowserError(Exception):
    pass


def _is_media_url(url: str, content_type: str = "") -> bool:
    """A network URL (or response) that looks like media.

    Content-Type catches extensionless manifests (tokenised HLS URLs
    often carry no .m3u8); the extension check catches plain files.
    """
    ctype = (content_type or "").lower()
    if ctype.startswith(("video/", "audio/")):
        return True
    if "mpegurl" in ctype or "dash+xml" in ctype:
        return True
    return bool(_MEDIA_RE.search(url or ""))


def _has_captcha(html: str, title: str) -> bool:
    low = ((html or "") + " " + (title or "")).lower()
    return any(marker in low for marker in _CAPTCHA_MARKERS)


def _pick_best(candidates):
    """candidates: list of (url, content_type, size_hint).

    A playlist (manifest) wins — it is what the player actually
    plays, and segment URLs (.ts) are useless without it. Then the
    biggest plain file. Else the first hit.
    """
    if not candidates:
        return None
    for url, ctype, _size in candidates:
        low = url.lower().split("?")[0]
        if "mpegurl" in (ctype or "").lower() \
                or "dash+xml" in (ctype or "").lower() \
                or low.endswith((".m3u8", ".mpd")):
            return url
    return max(candidates, key=lambda c: c[2] or 0)[0]


async def _check_media(page, network: dict, url: str):
    """Best media URL from what the page has shown so far, or None.

    Network responses first (a manifest wins), then the live DOM's
    video/audio/source elements, then the rendered HTML. Called once
    after the page loads and again after every play click — the
    player HTML only exists after the click on placeholder pages.
    """
    best = _pick_best([(u, c, s) for u, (c, s) in network.items()])
    if best:
        return best
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
            return src
    try:
        html = await page.content()
    except Exception:
        return None
    return player_scan.find_media(html, url)


async def _try_click_play(page, network: dict, url: str,
                          media_event: asyncio.Event):
    """Click play controls until media appears (or attempts run out).

    Returns the media URL, or None when no click produced one. Only
    the narrow _PLAY_SELECTORS list is ever clicked; a click that
    navigates the page away is undone with go_back, and popups are
    closed by the context handler in _visit.
    """
    clicked = 0
    for selector in _PLAY_SELECTORS:
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


async def _visit(url: str) -> str:
    """One browser visit: load the page, return the best media URL."""
    network: dict[str, tuple[str, int]] = {}  # url -> (ctype, size)
    media_event = asyncio.Event()

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
                prev = network.get(response.url)
                if prev is None or size > prev[1]:
                    network[response.url] = (ctype, size)
                    media_event.set()
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

            async def _close_popup(new_page):
                # A play click can open ad popups in new tabs; they
                # are never the video, so close them.
                try:
                    if new_page is not page:
                        await new_page.close()
                except Exception:
                    pass

            context.on("page", lambda p: asyncio.create_task(_close_popup(p)))
            try:
                await page.goto(url, wait_until="networkidle",
                                timeout=PAGE_TIMEOUT * 1000)
            except Exception:
                pass  # slow/polling pages: read what loaded so far
            html = await page.content()
            title = await page.title()
            if _has_captcha(html, title):
                raise BrowserError(
                    "that page needs a human check (captcha) before "
                    "it shows the video — download it in your browser "
                    "and send the file to the bot instead")
            found = await _check_media(page, network, url)
            if found:
                return found
            # Nothing yet — some players only load after a click.
            found = await _try_click_play(page, network, url, media_event)
            if found:
                return found
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


async def find_media_url(url: str) -> str:
    """Render `url` in a headless browser, return a media URL.

    Raises BrowserError when the browser is not installed, the page
    needs a captcha, the visit times out, or no media appears.
    """
    if not BROWSER_AVAILABLE:
        raise BrowserError(
            "the browser scraper isn't installed in this build")
    async with BROWSER_LOCK:
        try:
            return await asyncio.wait_for(_visit(url),
                                          timeout=BROWSER_TIMEOUT)
        except asyncio.TimeoutError:
            raise BrowserError(
                "the page took too long to load in the browser")
