"""Custom extractor: uploadhaven.com file links.

Why this exists: yt-dlp has no uploadhaven extractor, and the file
comes from a two-step form the generic gate walker cannot do: the
page's POST form must be submitted only after the site's 15-second
countdown, and the answer carries the link inside a success box
(<div class="alert alert-success mb-0"><a href="...">).

How it works:
  1. GET the file page (same session kept throughout — the POST
     needs the page's cookies).
  2. Wait 15 seconds for the site's countdown (async — the job
     slot is held, but nothing else is blocked).
  3. POST the form's fields back to the page.
  4. Read the link out of the success box.
  5. Download it directly.

Not verified live: only the homepage was reachable from this
sandbox, so the flow is replay-tested against the documented page
shape only. The first real link from the owner is the live proof.
"""

import asyncio
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import download_file, safe_filename
from core.extractors.player_scan import (HostError, forms, normalise_url,
                                         page_title, request)

#: The site's countdown before the form may be submitted.
_WAIT_SECONDS = 15

#: <div class="alert alert-success mb-0"><a href="...">
_SUCCESS_RE = re.compile(
    r"""<div[^>]*class=["']alert alert-success mb-0["'][^>]*>"""
    r"""\s*<a[^>]*href=["']([^"']+)["']""", re.I | re.S)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("uploadhaven.com", "www.uploadhaven.com"):
        return False
    return bool(urlparse(url).path.strip("/"))


def _post_fields(page: str) -> dict | None:
    """The page's POST form fields, or None when there is no form."""
    for form in forms(page):
        if form["method"] == "post" and form["fields"]:
            return form["fields"]
    return None


def _success_link(page: str, base_url: str) -> str | None:
    match = _SUCCESS_RE.search(page or "")
    if not match:
        return None
    return normalise_url(match.group(1), base_url)


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # One session for both steps: the form POST needs the
        # page's cookies.
        _final, page, status = await request(session, url)
        if status == 404:
            raise HostError(
                "uploadhaven: file not found — the link is dead or "
                "the file was removed")
        if status >= 400:
            raise HostError(f"uploadhaven answered HTTP {status}")
        fields = _post_fields(page)
        if not fields:
            raise HostError(
                "couldn't find the download form on the "
                "uploadhaven page — the file may have been "
                "removed, or the site changed its layout")
        title = page_title(page)
        await asyncio.sleep(_WAIT_SECONDS)
        _final2, page2, status2 = await request(
            session, url, method="POST", data=fields,
            headers={"Referer": url})
    if status2 >= 400:
        raise HostError(
            f"uploadhaven's download step answered HTTP {status2}")
    media_url = _success_link(page2, url)
    if not media_url:
        raise HostError(
            "uploadhaven didn't hand back a download link — try "
            "again in a bit")
    return {
        "title": title or "uploadhaven file",
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
    name = safe_filename(info["title"], "uploadhaven file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
