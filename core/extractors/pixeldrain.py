"""Custom extractor: pixeldrain.com file links.

Why this exists: yt-dlp has no pixeldrain extractor, and the share
page is JS-driven — the file itself is served by pixeldrain's public
API, which needs no key for downloads.

How it works:
  1. The file id is the last path segment of /u/<id> (or of a direct
     /api/file/<id> link).
  2. GET https://pixeldrain.com/api/file/<id>/info returns the real
     name, size and MIME type (no auth needed for public files).
  3. The file itself is GET https://pixeldrain.com/api/file/<id>
     (a redirect to the CDN; the downloader follows it).
  4. /l/<id> album links fail honestly — the bot sends one file per
     link, not whole albums.

Verified live 2026-11-18: info API 200 on a public file
("2026-11-17 08-57-27.mp4", 8,456,421 bytes) and a 1-byte Range GET
on the download URL answered 206 with the matching Content-Range.
Anonymous API uploads now need an API key (401), so no upload test
was possible — downloads are what the bot needs, and they work.
"""

import json
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import FallThrough, HostError

_API = "https://pixeldrain.com/api/file"


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("pixeldrain.com", "www.pixeldrain.com",
                    "pixeldra.in", "www.pixeldra.in"):
        return False
    path = urlparse(url).path
    return (path.startswith("/u/") or path.startswith("/l/")
            or path.startswith("/api/file/"))


def _file_id(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None


def _is_album(url: str) -> bool:
    return urlparse(url).path.startswith("/l/")


async def _get_json(url: str) -> tuple[int, dict | None]:
    """GET a JSON API URL; returns (status, parsed body or None)."""
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                    url, headers={"User-Agent": BROWSER_UA}) as resp:
                try:
                    return resp.status, await resp.json(
                        content_type=None)
                except (ValueError, json.JSONDecodeError):
                    return resp.status, None
    except Exception as exc:
        raise HostError(f"couldn't reach pixeldrain ({exc})") from exc


def _parse_info(data: dict | None, file_id: str) -> dict:
    """Turn an /info payload into the resolve() dict (or raise)."""
    if not data or not data.get("success"):
        value = (data or {}).get("value", "")
        if value == "not_found":
            raise HostError(
                "pixeldrain: file not found — the link is dead or "
                "the file was removed")
        raise HostError(
            "pixeldrain couldn't look up that file "
            f"({value or 'unknown error'})")
    if not data.get("can_download", True):
        raise HostError(
            "pixeldrain won't let this file be downloaded — it may "
            "be locked for review")
    name = data.get("name") or file_id
    return {
        "title": name,
        "media_url": f"{_API}/{file_id}",
        "approx_size": int(data.get("size") or 0),
        "is_playlist": False,
    }


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    file_id = _file_id(url)
    if not file_id:
        raise FallThrough("that pixeldrain link has no file id in it")
    if _is_album(url):
        raise HostError(
            "that pixeldrain link is an album, not one file — send "
            "a single file's link instead")
    _status, data = await _get_json(f"{_API}/{file_id}/info")
    return _parse_info(data, file_id)


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    # cancel_check is accepted for the extractor interface but
    # unused here: this module downloads directly, and a direct
    # download aborts when progress_cb raises JobCancelled.
    """Resolve the link and download the file; returns file paths."""
    info = await resolve(url)
    name = safe_filename(info["title"], "pixeldrain file")
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
