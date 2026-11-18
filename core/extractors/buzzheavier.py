"""Custom extractor: buzzheavier.com file links (and bzzhr.co/.to).

Why this exists: yt-dlp has no buzzheavier extractor, and the page
is an htmx app — the download button is an <a hx-get="...download...">
whose target 302s to the file. No form, no player, nothing the
generic gate walker can drive.

How it works:
  1. GET the file page (the site Cloudflare-challenges plain
     requests; the shared fetch falls back to curl_cffi, which
     passes it).
  2. Read the hx-get download path out of the page.
  3. GET it without following redirects — the Location header is
     the file's URL on their storage.
  4. Folder pages (a table of files) fail honestly — the bot sends
     one file per link.

Not verified live: the site answers Cloudflare's challenge page to
this sandbox, so the flow is replay-tested against the page shape
only. The first real link from the owner is the live proof.
"""

import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import (HostError, fetch_page, normalise_url,
                                         page_title)

_HOSTS = ("buzzheavier.com", "www.buzzheavier.com",
          "bzzhr.co", "www.bzzhr.co", "bzzhr.to", "www.bzzhr.to")

#: The download button: <a hx-get="/<id>/download" ...>.
_HX_GET_RE = re.compile(r"""<a[^>]*hx-get=["']([^"']*download[^"']*)["']""",
                        re.I)
#: A folder page lists its files in a table body.
_FOLDER_MARK = '<tbody id="tbody"'


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in _HOSTS:
        return False
    path = urlparse(url).path.strip("/")
    return bool(path)


def _hx_get_path(page: str) -> str | None:
    match = _HX_GET_RE.search(page or "")
    return match.group(1) if match else None


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


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    _final, page = await fetch_page(url)
    if _FOLDER_MARK in page:
        raise HostError(
            "that buzzheavier link is a folder, not one file — send "
            "a single file's link instead")
    path = _hx_get_path(page)
    if not path:
        raise HostError(
            "couldn't find the download button on the buzzheavier "
            "page — the file may have been removed, or the site "
            "changed its layout")
    direct = await _location_no_redirect(normalise_url(path, url), url)
    if not direct:
        raise HostError(
            "buzzheavier's download step gave no file link — try "
            "again in a bit")
    return {
        "title": page_title(page) or urlparse(url).path.strip("/"),
        "media_url": direct,
        "approx_size": 0,
        "is_playlist": False,
    }


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    # cancel_check is accepted for the extractor interface but
    # unused here: this module downloads directly, and a direct
    # download aborts when progress_cb raises JobCancelled.
    """Resolve the link and download the file; returns file paths."""
    info = await resolve(url)
    name = safe_filename(info["title"], "buzzheavier file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
