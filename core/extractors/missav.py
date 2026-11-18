"""Custom extractor: MissAV (missav.ws / missav.live video pages).

Why this exists: a MissAV page is not a player page yt-dlp can read, and
the stream URL is never printed in plain HTML. The page ships an
obfuscated (Dean Edwards "packed") player script whose source is a
``source: 'https://surrit.com/<uuid>/<quality>.m3u8'`` line; the
qualities are 360p/480p/720p/1080p next to a ``playlist.m3u8`` master.
Segments come back named ``video0.jpeg`` … but they are MPEG-TS and
none of them is encrypted — a plain HLS download works, it only has to
carry the page's ``Referer`` (and ``Origin``) to surrit.

Cloudflare: from a datacenter IP the site often answers the challenge
page ("Just a moment…", HTTP 403) instead of the article. The page
fetch below tries a normal request, then curl_cffi with Chrome's TLS
fingerprint; when even that is challenged the extractor says so
plainly instead of pretending the link is broken. A real browser
(``/browser``) is the fallback the owner has for that case.

Verified 2026-11-18: the mechanism (packed JS → ``surrit.com/<uuid>/
<quality>.m3u8``, TS-over-.jpeg segments, no encryption, Referer
required). NOT verified live: this sandbox's IP only ever receives
Cloudflare's challenge page, so the parser was tested against the
known page shape, not a freshly fetched article.
"""

import os
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA
from core.extractors.player_scan import (HostError, fetch_partial,
                                         page_title, scripts, unpack_packed)

#: Hosts that serve MissAV article pages.
HOSTS = (
    "missav.ws", "www.missav.ws",
    "missav.live", "www.missav.live",
    "missav.com", "www.missav.com",
)

#: Page budget: the packed player sits in the body, and the page is a
#: few hundred KB. Reading until "surrit" appears keeps it quick.
_PAGE_BUDGET = 900 * 1024
_PAGE_TIMEOUT = 30

_SURRIT_RE = re.compile(
    r"https?://surrit\.com/([0-9a-fA-F-]{16,})/([A-Za-z0-9_.-]+\.m3u8)")
_M3U8_RE = re.compile(r"https?://[^\"'\\\s]+\.m3u8[^\"'\\\s]*")
_UUID_RE = re.compile(r"surrit\.com/([0-9a-fA-F-]{16,})")
_SOURCE_RE = re.compile(r"surrit\.com/[0-9a-fA-F-]{16,}/[A-Za-z0-9_.-]+")

#: Tried in order when the page names a uuid but no playlist file.
_QUALITIES = ("playlist.m3u8", "1080p.m3u8", "720p.m3u8",
              "480p.m3u8", "360p.m3u8")

#: Titles end with the site name; the file name shouldn't.
_TITLE_TAIL = re.compile(
    r"\s*[-|]\s*(?:MissAV|MissAv|missav)(?:\.(?:ws|live|com))?\s*$", re.I)


def is_site_url(url: str) -> bool:
    return (urlparse(url).netloc or "").lower() in HOSTS


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc or 'missav.ws'}"


def _clean_title(title: str, url: str) -> str:
    title = _TITLE_TAIL.sub("", (title or "").strip()).strip()
    if not title:
        parts = [p for p in urlparse(url).path.split("/") if p]
        title = parts[-1] if parts else "missav video"
    return title


def _decode(page: str) -> str:
    """The page with every packed script expanded in place."""
    parts = [page]
    for script in scripts(page):
        unpacked = unpack_packed(script)
        if unpacked:
            parts.append(unpacked)
    return "\n".join(parts)


def _playlists(text: str) -> list[str]:
    found: list[str] = []
    for match in _SURRIT_RE.finditer(text):
        url = match.group(0)
        if url not in found:
            found.append(url)
    if found:
        # A master playlist first: it carries every quality.
        found.sort(key=lambda u: "playlist.m3u8" not in u)
        return found
    for match in _M3U8_RE.finditer(text):
        url = match.group(0)
        if "surrit.com" in url and url not in found:
            found.append(url)
    return found


def _uuid(text: str) -> str | None:
    match = _UUID_RE.search(text)
    return match.group(1) if match else None


async def _probe(url: str, referer: str) -> bool:
    """True when `url` answers a real HLS playlist.

    One byte of Range is enough: the CDN replies to a manifest with
    ``#EXTM3U`` and to a missing quality with an HTML error page.
    """
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*",
               "Referer": referer, "Range": "bytes=0-2047"}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers,
                                   allow_redirects=True) as resp:
                if resp.status >= 400:
                    return False
                body = (await resp.content.read(2048)).decode(
                    "utf-8", "replace")
                return "#EXTM3U" in body
    except Exception:
        return False


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist, referer}."""
    headers = {"User-Agent": BROWSER_UA,
               "Accept": "text/html,application/xhtml+xml,application/xml;"
                         "q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    try:
        final_url, page = await fetch_partial(
            url, needle="surrit", max_bytes=_PAGE_BUDGET,
            timeout=_PAGE_TIMEOUT, headers=headers)
    except HostError as exc:
        text = str(exc)
        if "403" in text or "challenge" in text.lower():
            raise HostError(
                "missav is behind Cloudflare's browser check for the bot's "
                "IP — send the link with /browser, or download it in your "
                "browser") from exc
        raise
    if not page.strip():
        raise HostError("missav returned an empty page")

    text = _decode(page)
    candidates = _playlists(text)
    referer = final_url or url

    if not candidates:
        uuid = _uuid(text)
        if not uuid:
            raise HostError(
                "couldn't find the stream on that missav page — the site "
                "may have changed its player")
        candidates = [f"https://surrit.com/{uuid}/{name}"
                      for name in _QUALITIES]

    for candidate in candidates:
        if await _probe(candidate, referer):
            return {
                "title": _clean_title(page_title(page), url),
                "media_url": candidate,
                "approx_size": 0,
                "is_playlist": True,
                "referer": referer,
            }
    raise HostError(
        "missav listed a stream, but its host refused every playlist "
        "quality — the link may have expired, try again in a moment")


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    info = await resolve(url)
    referer = info.get("referer") or url
    parsed = urlparse(referer)
    headers = {"Referer": referer, "Origin": _origin(url),
               "User-Agent": BROWSER_UA}
    from core.extractors.ytdlp_engine import download as ytdlp_download
    paths = await ytdlp_download(info["media_url"], job_dir,
                                 progress_cb=progress_cb, headers=headers,
                                 cancel_check=cancel_check)
    from core.extractors.direct import safe_filename
    title = safe_filename(info["title"], "missav video")
    video = [p for p in paths
             if os.path.splitext(p)[1].lower() in
             (".mp4", ".mkv", ".webm", ".ts")]
    if video:
        ext = os.path.splitext(video[0])[1]
        dest = os.path.join(job_dir, title + ext)
        if dest != video[0]:
            try:
                os.rename(video[0], dest)
                paths[paths.index(video[0])] = dest
            except OSError:
                pass
    if not paths:
        raise HostError(f"missav gave me a stream but nothing downloaded "
                        f"from {parsed.netloc}")
    return paths
