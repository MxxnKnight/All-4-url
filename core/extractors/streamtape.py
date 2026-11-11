"""Custom extractor: Streamtape and its clone domains.

Why this exists: yt-dlp has no Streamtape extractor any more, the page
carries no <video> tag, no og:video and no direct link in its HTML — the
real CDN path is assembled in JavaScript from an obfuscated string:

    document.getElementById('norobotlink').innerHTML =
        '//streamtape.com/get_video?i'
        + ('xcdd=FILE&expires=...&ip=...&token=...').substring(1).substring(2);

Replaying that substring arithmetic (see
``player_scan.eval_js_string``) yields
``//streamtape.com/get_video?id=FILE&expires=...&token=...``, which
answers with a 302 to ``NNN.tapecontent.net/radosgw/<file>``. That CDN
link works from the bot's own IP — it is generated for this request, not
copied from someone else's browser — so no browser and no cookies are
needed anywhere in this flow.

Verified live on 2026-11-11 against
``https://streamtape.com/v/LQYwpG81BwiRGOv``: page → get_video → 302 →
tapecontent.net, HTTP 200 with ``Content-Length``.

Domains: streamtape.com and the mirrors that run the same panel
(tapeadsenjoyer.com is byte-identical to streamtape.com's home page).
A mirror that stops using this player simply fails on its own; the
registry keeps it isolated from every other site.
"""

import os
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import (HostError, eval_js_string, fetch_page,
                                         normalise_url, page_title)

BROWSER_HOSTS = (
    "streamtape.com", "www.streamtape.com",
    "tapeadsenjoyer.com", "www.tapeadsenjoyer.com",
    "streamtape.to", "www.streamtape.to",
    "streamtape.net", "www.streamtape.net",
    "foradswithtape.com", "www.foradswithtape.com",
)

#: The DOM ids Streamtape has used for the player link, best first.
_LINK_IDS = ("norobotlink", "botlink", "ideoooolink", "ideoolink")

_EXPR_RE = re.compile(
    r"getElementById\(\s*['\"](%s)['\"]\s*\)\s*\.innerHTML\s*=\s*(.+?);"
    % "|".join(_LINK_IDS), re.S)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in BROWSER_HOSTS


def _file_code(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise HostError("that streamtape link has no file code in it")
    # /v/<code> and /e/<code> are the two shapes; a bare /<code> works too.
    code = parts[1] if len(parts) > 1 and parts[0] in ("v", "e") else parts[0]
    code = code.split(".")[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code or ""):
        raise HostError("that streamtape link has no usable file code in it")
    return code


def _view_url(url: str) -> str:
    """Normalise to the /v/ page — the embed /e/ page hides the link."""
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}/v/{_file_code(url)}"


def _player_link(page: str, page_url: str) -> str:
    """Replay the page's own obfuscation to get the get_video URL."""
    best = None
    for dom_id, expr in _EXPR_RE.findall(page):
        raw = eval_js_string(expr)
        if not raw:
            continue
        link = normalise_url(raw, page_url)
        if "get_video" not in link:
            continue
        if dom_id == "norobotlink":
            return link  # the one the player itself uses
        best = best or link
    if best:
        return best
    raise HostError(
        "couldn't find the video link in the streamtape page — the site "
        "may have changed its player")


async def _gate_link(link: str, page_url: str) -> str:
    """Resolve get_video's 302 to the CDN URL (and check it is alive)."""
    headers = {"User-Agent": BROWSER_UA, "Referer": page_url}
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(link, headers=headers,
                                   allow_redirects=False) as resp:
                status = resp.status
                location = resp.headers.get("Location") or ""
                if status in (301, 302, 303, 307) and location:
                    return normalise_url(location, page_url)
                if status == 200:
                    # Some mirrors serve the bytes straight from get_video.
                    return link
                raise HostError(
                    f"streamtape refused the download (HTTP {status}) — "
                    "the link may be dead or IP-locked")
    except HostError:
        raise
    except Exception as exc:
        raise HostError(f"couldn't resolve the streamtape CDN link ({exc})") from exc


async def _size_of(media_url: str, referer: str) -> int:
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(media_url,
                                    headers={"User-Agent": BROWSER_UA,
                                             "Referer": referer},
                                    allow_redirects=True) as resp:
                return int(resp.headers.get("Content-Length") or 0)
    except Exception:
        return 0


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    page_url = _view_url(url)
    _, page = await fetch_page(page_url)
    if "not found" in page.lower() and "file" in page.lower() \
            and "get_video" not in page:
        raise HostError("the streamtape file was not found — dead link")
    title = page_title(page)
    gate = _player_link(page, page_url)
    media_url = await _gate_link(gate, page_url)
    return {
        "title": title,
        "media_url": media_url,
        "approx_size": await _size_of(media_url, page_url),
        "is_playlist": False,
        "referer": page_url,
    }


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    info = await resolve(url)
    title = safe_filename(info["title"], "streamtape video")
    media_url = info["media_url"]
    ext = os.path.splitext(urlparse(media_url).path)[1].lower() or ".mp4"
    if ext not in (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".ts"):
        ext = ".mp4"
    return [await download_file(
        media_url, job_dir, progress_cb=progress_cb,
        filename=title + ext,
        headers={"Referer": info.get("referer") or url})]
