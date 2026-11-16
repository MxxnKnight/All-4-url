"""Custom extractor: mediafire.com file pages.

Why this exists: yt-dlp has no mediafire extractor, and the page is
not a direct file — the real download URL is minted per request and
lives in the page's download button. Reading the button out of the
HTML is all the bot needs; no account, no browser.

How it works, in order:
  1. A URL that already points at mediafire's download CDN
     (``download<N>.mediafire.com``) is used as-is.
  2. Otherwise GET the file page (plain HTTP, browser UA).
  3. The download button is the ``aria-label="Download file"``
     anchor; if the page shape changes, fall back to any
     ``download<N>.mediafire.com`` URL in the HTML.
  4. If the page shows a "retry" link instead (stale page),
     follow it to the repair page and read the ``continue-btn``
     link there, then extract again.
  5. Password-protected files fail honestly — the bot has no
     password-entry flow, so it says so instead of guessing.

Verified live 2026-11-16 on a public 2.67 MB zip: page fetch,
button extraction, and a Range GET on the minted URL all work
from the sandbox (HTTP 206, zip bytes).
"""

import re
from urllib.parse import unquote, urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import FallThrough, HostError, fetch_page

#: The CDN host a minted download URL lives on.
_DIRECT_RE = re.compile(r"https?://download\d+\.mediafire\.com/\S+")
#: The download button (href may come before or after the label).
_BUTTON_RE = re.compile(
    r'<a[^>]*aria-label="Download file"[^>]*href="([^"]+)"'
    r'|<a[^>]*href="([^"]+)"[^>]*aria-label="Download file"')
#: The "this page is stale, try again" link.
_RETRY_RE = re.compile(r"""<a[^>]*class=['"]retry['"][^>]*href="([^"]+)\"""")
#: The repair page's onward link.
_CONTINUE_RE = re.compile(
    r"""<a[^>]*id=['"]continue-btn['"][^>]*href="([^"]+)\"""")
#: The site's own error line on dead/removed files.
_ERROR_RE = re.compile(r'<p class="notranslate">([^<]+)</p>')
#: A real password prompt (the string also appears in the page's
#: CSS, so only the div counts).
_PASSWORD_DIV = '<div class="passwordPrompt"'


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host == "mediafire.com" or host.endswith(".mediafire.com")


def _clean_found(raw: str) -> str:
    # Undo HTML escaping and drop a trailing quote the regex may
    # have swallowed from the href="..." attribute. The URL's own
    # %-escapes are left alone — they are part of the address.
    return raw.replace("&amp;", "&").strip().rstrip("\"'")


def _direct_from_html(html: str) -> str | None:
    match = _BUTTON_RE.search(html)
    if match:
        return _clean_found(match.group(1) or match.group(2))
    match = _DIRECT_RE.search(html)
    if match:
        return _clean_found(match.group(0))
    return None


def _title_from_html(html: str, fallback: str) -> str:
    match = re.search(r"<title>([^<]+)</title>", html, re.I)
    if match:
        title = match.group(1).strip()
        title = re.sub(r"\s*-\s*MediaFire\s*$", "", title, flags=re.I)
        if title:
            return title
    return fallback


async def _probe_size(url: str) -> int:
    """Best-effort byte size: 1-byte Range GET, else HEAD. Never raises."""
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                    url, headers={"User-Agent": BROWSER_UA,
                                  "Range": "bytes=0-0"}) as resp:
                if resp.status == 206:
                    total = resp.headers.get("Content-Range", "").rsplit(
                        "/", 1)[-1]
                    await resp.read()
                    return int(total) if total.isdigit() else 0
                if resp.status == 200:
                    await resp.read()
                    length = resp.headers.get("Content-Length", "")
                    return int(length) if length.isdigit() else 0
            async with session.head(
                    url, headers={"User-Agent": BROWSER_UA},
                    allow_redirects=True) as resp:
                length = resp.headers.get("Content-Length", "")
                return int(length) if length.isdigit() else 0
    except Exception:
        return 0


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    if _DIRECT_RE.match(url):
        # Already a minted download URL — use it as-is.
        name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "mediafire file"
        return {"title": name, "media_url": url,
                "approx_size": await _probe_size(url), "is_playlist": False}
    if "/folder/" in urlparse(url).path:
        raise FallThrough(
            "that mediafire link is a folder, not a single file — "
            "send one file's link instead")
    _, html = await fetch_page(url)
    if _PASSWORD_DIV in html:
        raise HostError(
            "this mediafire file needs a password to download, and "
            "the bot has no way to ask for one")
    media_url = _direct_from_html(html)
    if not media_url:
        # Stale page: follow the retry link to the repair page.
        retry = _RETRY_RE.search(html)
        if retry:
            repair = retry.group(1).replace("&amp;", "&")
            if repair.startswith("/"):
                repair = "https://www.mediafire.com" + repair
            _, repair_html = await fetch_page(repair)
            cont = _CONTINUE_RE.search(repair_html)
            if cont:
                target = cont.group(1).replace("&amp;", "&")
                if target.startswith("/"):
                    target = "https://www.mediafire.com" + target
                _, html = await fetch_page(target)
                media_url = _direct_from_html(html)
    if not media_url:
        err = _ERROR_RE.search(html)
        if err and err.group(1).strip():
            raise HostError(f"mediafire says: {err.group(1).strip()}")
        raise HostError(
            "couldn't find the download button on the mediafire "
            "page — the site may have changed its layout, or the "
            "file may have been removed")
    if media_url.startswith("//"):
        media_url = "https:" + media_url
    fallback = unquote(urlparse(media_url).path.rsplit("/", 1)[-1])
    title = _title_from_html(html, fallback or "mediafire file")
    return {"title": title, "media_url": media_url,
            "approx_size": await _probe_size(media_url),
            "is_playlist": False}


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    """Resolve the page and download the file; returns file paths."""
    info = await resolve(url)
    name = safe_filename(info["title"], "mediafire file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(info["media_url"], job_dir,
                                progress_cb=progress_cb, filename=name)]
