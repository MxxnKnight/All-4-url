"""Custom extractor: upload.ee file links.

Why this exists: yt-dlp has no upload.ee extractor, and the file
page's download button is a plain anchor with id="d_l" — the direct
URL is right there in the HTML, no form to submit and no player to
read.

How it works:
  1. GET the file page.
  2. Read the href of the <a id="d_l"> download anchor.
  3. Download it directly.

Not verified live: only the homepage was reachable from this
sandbox, so the flow is replay-tested against the documented page
shape only. The first real link from the owner is the live proof.
"""

import re
from urllib.parse import urlparse

from core.extractors.direct import download_file, safe_filename
from core.extractors.player_scan import (HostError, fetch_page,
                                         normalise_url, page_title)

#: <a id="d_l" href="https://upload.ee/download/...">
_DL_RE = re.compile(
    r"""<a[^>]*id=["']d_l["'][^>]*href=["']([^"']+)["']""", re.I)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("upload.ee", "www.upload.ee"):
        return False
    return bool(urlparse(url).path.strip("/"))


def _dl_url(page: str, base_url: str) -> str | None:
    match = _DL_RE.search(page or "")
    if not match:
        return None
    return normalise_url(match.group(1), base_url)


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    _final, page = await fetch_page(url)
    media_url = _dl_url(page, url)
    if not media_url:
        raise HostError(
            "couldn't find the download link on the upload.ee page "
            "— the file may have been removed, or the site changed "
            "its layout")
    return {
        "title": page_title(page) or "upload.ee file",
        "media_url": media_url,
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
    name = safe_filename(info["title"], "upload.ee file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
