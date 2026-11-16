"""Last resort after the static scrape — a real headless browser.

Some pages only reveal their video URL after JavaScript runs: the
player config is fetched, decrypted, or assembled at runtime, so no
amount of static HTML scanning finds it. Playwright drives a real
headless Chromium, loads the page, and watches what the page itself
requests — the same idea as the Ghost Downloader extension's network
sniffing, minus the extension.

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


async def _visit(url: str) -> str:
    """One browser visit: load the page, return the best media URL."""
    network: dict[str, tuple[str, int]] = {}  # url -> (ctype, size)

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
            best = _pick_best([(u, c, s)
                               for u, (c, s) in network.items()])
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
            found = player_scan.find_media(html, url)
            if found:
                return found
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
