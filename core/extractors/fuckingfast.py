"""Custom extractor: fuckingfast.co file links.

Why this exists: yt-dlp has no fuckingfast extractor, and the page
hides the file behind a scripted button — the download URL sits in
a window.open('https://fuckingfast.co/dl/...') call in the page's
JavaScript. No form, no player, nothing the generic gate walker
can drive.

How it works:
  1. GET the file page (the site Cloudflare-challenges plain
     requests; the shared fetch falls back to curl_cffi, which
     passes it).
  2. Read the /dl/ URL out of the window.open call.
  3. Download it directly.

Not verified live: the site answers Cloudflare's challenge page to
this sandbox, so the flow is replay-tested against the page shape
only. The first real link from the owner is the live proof.
"""

import re
from urllib.parse import urlparse

from core.extractors.direct import download_file, safe_filename
from core.extractors.player_scan import (HostError, fetch_page, page_title)

#: window.open('https://fuckingfast.co/dl/<token>')
_DL_RE = re.compile(
    r"""window\.open\(\s*["'](https://fuckingfast\.co/dl/[^"']+)["']""",
    re.I)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("fuckingfast.co", "www.fuckingfast.co"):
        return False
    return bool(urlparse(url).path.strip("/"))


def _dl_url(page: str) -> str | None:
    match = _DL_RE.search(page or "")
    return match.group(1) if match else None


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    _final, page = await fetch_page(url)
    media_url = _dl_url(page)
    if not media_url:
        raise HostError(
            "couldn't find the download link on the fuckingfast "
            "page — the file may have been removed, or the site "
            "changed its layout")
    return {
        "title": page_title(page) or "fuckingfast file",
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
    name = safe_filename(info["title"], "fuckingfast file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
