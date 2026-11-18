"""Custom extractor: FireStream (firestream.to / frstream.life).

Why this exists: the watch page is a client-rendered player and the
stream URL is never in its HTML. The page ships a one-time token in
a ``<script id="token-blob">`` element; the page's own JavaScript
trades it for a signed stream:

    POST /api/videos/<slug>/resolve
    {"blob": "<one-time token>"}
      -> {"signedVideoUrl": "<m3u8>", "signedVideoMp4Url": "<mp4>", ...}

The signed URLs are minted for the requesting IP, so they play from
the bot with no browser and no account. Both hosts run the same app;
``firestream.to`` answers a 302 to ``frstream.life`` and the bot
follows that on the first request (the final URL is what the resolve
call is sent to).

One more thing matters here: these pages are enormous (800 KB+) and
arrive slowly — measured 2026-11-18: 21 KB in 5 s, the whole page
never finishing inside a 45 s budget. The token sits near the top
(~20 KB), so the extractor reads only a byte budget and stops as
soon as the token is found. Reading the whole page is what used to
fail (the bot's scrape step has a 25 s cap).

Verified live on 2026-11-18 against
``https://firestream.to/e/GEfPxSgH``: 302 → frstream.life, token
found at byte ~20 471, resolve answered 200 with a
``signedVideoUrl`` on ``fr-cdn-vs-0.firestream.to`` and a
``signedVideoMp4Url`` beside it.
"""

import os
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import (HostError, fetch_partial,
                                         page_title)

#: Hosts that run this player (the .to link redirects to .life).
HOSTS = (
    "firestream.to", "www.firestream.to",
    "frstream.life", "www.frstream.life",
    "firestream.co", "www.firestream.co",
)

#: The one-time token element (its class/attribute names vary little).
_BLOB_RE = re.compile(
    r"""<script[^>]*(?:id|class)=["'][^"']*token[^"']*["'][^>]*>(.*?)</script>""",
    re.S | re.I)
_BLOB_FALLBACK_RE = re.compile(
    r"""id=["']token-blob["'][^>]*>(.*?)</""", re.S | re.I)

#: Read budget for the page: the token is near the top, the rest is
#: player code and ads we never look at.
_PAGE_BUDGET = 400 * 1024
_PAGE_TIMEOUT = 25


def is_site_url(url: str) -> bool:
    return (urlparse(url).netloc or "").lower() in HOSTS


def _slug(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if not parts:
        raise HostError("that firestream link has no video id in it")
    slug = parts[-1]
    for suffix in (".html", ".htm"):
        if slug.lower().endswith(suffix):
            slug = slug[: -len(suffix)]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug or ""):
        raise HostError("that firestream link has no usable video id in it")
    return slug


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _token(page: str) -> str | None:
    for pattern in (_BLOB_FALLBACK_RE, _BLOB_RE):
        match = pattern.search(page or "")
        if not match:
            continue
        token = (match.group(1) or "").strip()
        # Strip a CDATA wrapper / HTML comment if the shell has one.
        token = re.sub(r"^(?:<!\[CDATA\[|<!--)|(?:-->|\]\]>)$", "",
                       token).strip()
        if token:
            return token
    return None


async def _resolve_signed(page_url: str, token: str) -> dict:
    """Trade the page's token for the signed stream URLs."""
    origin = _origin(page_url)
    slug = _slug(page_url)
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer": page_url,
        "Origin": origin,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    timeout = aiohttp.ClientTimeout(total=35)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                    f"{origin}/api/videos/{slug}/resolve",
                    json={"blob": token}, headers=headers) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    raise HostError(
                        f"firestream's resolve step answered with a web "
                        f"page (HTTP {status}), not JSON — the site may be "
                        f"down or behind a browser check")
    except HostError:
        raise
    except Exception as exc:
        raise HostError(
            f"couldn't ask firestream for the stream ({exc})") from exc
    if not isinstance(data, dict):
        raise HostError("firestream's resolve step returned an unexpected reply")
    return data


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    final_url, page = await fetch_partial(
        url, needle="token", max_bytes=_PAGE_BUDGET, timeout=_PAGE_TIMEOUT,
        headers={"User-Agent": BROWSER_UA})
    token = _token(page)
    if not token:
        raise HostError(
            "couldn't find firestream's playback token in the page — "
            "the site may have changed its player")
    data = await _resolve_signed(final_url or url, token)
    playlist = data.get("signedVideoUrl") or data.get("signedVideoHlsUrl")
    mp4 = data.get("signedVideoMp4Url") or data.get("signedVideoSdUrl")
    media = playlist or mp4
    if not media:
        reason = data.get("error") or data.get("detail") or "no stream returned"
        raise HostError(f"firestream wouldn't play this video ({reason})")
    is_playlist = bool(playlist) or media.lower().split("?")[0].endswith(".m3u8")
    title = (data.get("title") or data.get("name")
             or page_title(page) or f"firestream {_slug(url)}")
    return {
        "title": title,
        "media_url": media,
        "approx_size": 0,
        "is_playlist": is_playlist,
        "referer": final_url or url,
    }


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    info = await resolve(url)
    title = safe_filename(info["title"], "firestream video")
    media_url = info["media_url"]
    referer = info.get("referer") or url
    if info["is_playlist"]:
        from core.extractors.ytdlp_engine import download as ytdlp_download
        paths = await ytdlp_download(media_url, job_dir,
                                     progress_cb=progress_cb,
                                     headers={"Referer": referer},
                                     cancel_check=cancel_check)
        media = [p for p in paths
                 if os.path.splitext(p)[1].lower() in
                 (".mp4", ".mkv", ".webm", ".ts")]
        if media:
            ext = os.path.splitext(media[0])[1]
            dest = os.path.join(job_dir, title + ext)
            if dest != media[0]:
                try:
                    os.rename(media[0], dest)
                    paths[paths.index(media[0])] = dest
                except OSError:
                    pass
        return paths
    ext = os.path.splitext(urlparse(media_url).path)[1].lower() or ".mp4"
    return [await download_file(media_url, job_dir, progress_cb=progress_cb,
                                filename=title + ext,
                                headers={"Referer": referer})]
