"""Custom extractor: sourceforge.net project file links.

Why this exists: yt-dlp has no SourceForge extractor, and the file
is behind a mirror picker: the /download page answers with a meta
refresh to downloads.sourceforge.net, which 302s to a mirror.

How it works:
  1. GET the /download page (SourceForge puts plain requests behind
     a Cloudflare check; the shared fetch falls back to curl_cffi,
     which passes it).
  2. Read the meta-refresh URL out of the page.
  3. GET it without following redirects — the Location header is the
     mirror's file URL (time-limited, so it is minted fresh on every
     resolve).
  4. The mirror URL is downloaded directly.

Verified live 2026-11-18 end to end on 7-Zip 24.09's x64 exe:
page 200 with meta refresh, mirror 302, Range GET 206 with
1,637,343 bytes total.
"""

import html as html_lib
import re
from urllib.parse import unquote, urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import (FallThrough, HostError, fetch,
                                         normalise_url)

#: The mirror picker's meta refresh: content="5; url=https://...".
_META_RE = re.compile(
    r"""http-equiv=["']refresh["'][^>]*content=["'][^"']*?url=([^"'\s>]+)""",
    re.I)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("sourceforge.net", "www.sourceforge.net"):
        return False
    path = urlparse(url).path
    return "/projects/" in path and (
        "/files/" in path or path.rstrip("/").endswith("/download"))


def _download_page_url(url: str) -> str:
    if url.rstrip("/").endswith("/download"):
        return url
    return url.rstrip("/") + "/download"


def _meta_refresh_url(page: str, base_url: str) -> str | None:
    match = _META_RE.search(page or "")
    if not match:
        return None
    return normalise_url(html_lib.unescape(match.group(1)), base_url)


def _title_from_path(url: str) -> str:
    path = urlparse(url).path
    if "/files/" in path:
        name = path.split("/files/", 1)[1].rsplit("/download", 1)[0]
        name = unquote(name.rstrip("/").rsplit("/", 1)[-1])
        if name:
            return name
    return unquote(path.rstrip("/").rsplit("/", 1)[-1]) or "sourceforge file"


async def _location_no_redirect(url: str, referer: str) -> str | None:
    """GET without following redirects; returns the Location or None."""
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                    url, allow_redirects=False,
                    headers={"User-Agent": BROWSER_UA,
                             "Referer": referer}) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    return resp.headers.get("Location")
                return None
    except Exception:
        return None


async def _probe_size(url: str) -> int:
    """Best-effort byte size: 1-byte Range GET. Never raises."""
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                    url, headers={"User-Agent": BROWSER_UA,
                                  "Range": "bytes=0-0"}) as resp:
                await resp.read()
                if resp.status == 206:
                    total = resp.headers.get("Content-Range", "").rsplit(
                        "/", 1)[-1]
                    return int(total) if total.isdigit() else 0
                length = resp.headers.get("Content-Length", "")
                return int(length) if length.isdigit() else 0
    except Exception:
        return 0


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    if "/files/" not in urlparse(url).path:
        raise FallThrough(
            "that sourceforge link is a project page, not a file — "
            "open the file itself and send its link")
    page_url = _download_page_url(url)
    _final, page, status = await fetch(page_url, headers={
        "Referer": page_url.rsplit("/", 2)[0] + "/"})
    if status == 404:
        raise HostError(
            "sourceforge: file not found — the link is dead or the "
            "file was removed")
    if status >= 400:
        raise HostError(f"sourceforge answered HTTP {status}")
    meta_url = _meta_refresh_url(page, page_url)
    if not meta_url:
        raise HostError(
            "couldn't find the mirror link on the sourceforge page "
            "— the file may have been removed, or the site changed "
            "its layout")
    mirror = await _location_no_redirect(meta_url, page_url)
    if not mirror:
        raise HostError(
            "sourceforge's mirror picker gave no download link — "
            "try again in a bit")
    title = _title_from_path(url)
    return {
        "title": title,
        "media_url": mirror,
        "approx_size": await _probe_size(mirror),
        "is_playlist": False,
    }


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    # cancel_check is accepted for the extractor interface but
    # unused here: this module downloads directly, and a direct
    # download aborts when progress_cb raises JobCancelled.
    """Resolve the link and download the file; returns file paths."""
    info = await resolve(url)
    name = safe_filename(info["title"], "sourceforge file")
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name)]
