"""Custom extractor: krakenfiles.com file links.

Why this exists: yt-dlp has no krakenfiles extractor, and the file
comes from a small token dance the generic gate walker cannot do:
the page carries a form (#dl-form) with a one-time token
(#dl-token); POSTing the token returns JSON {"status": "ok",
"url": "..."} with the file's URL.

How it works:
  1. GET the file page (same session kept throughout — the POST
     needs the page's cookies).
  2. Read the form's action and the token out of the page.
  3. POST the token, read the URL out of the JSON reply.
  4. Download it directly.

Not verified live: only the homepage was reachable from this
sandbox, so the flow is replay-tested against the documented page
shape only. The first real link from the owner is the live proof.
"""

import json
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import download_file, safe_filename
from core.extractors.player_scan import (HostError, normalise_url,
                                         page_title, request)

#: <form id="dl-form" action="/dl/..."> — the token endpoint.
_FORM_RE = re.compile(
    r"""<form[^>]*id=["']dl-form["'][^>]*action=["']([^"']+)["']""", re.I)
#: <input id="dl-token" value="..."> — the one-time token.
_TOKEN_RE = re.compile(
    r"""<input[^>]*id=["']dl-token["'][^>]*value=["']([^"']+)["']""", re.I)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("krakenfiles.com", "www.krakenfiles.com"):
        return False
    return bool(urlparse(url).path.strip("/"))


def _form_action(page: str) -> str | None:
    match = _FORM_RE.search(page or "")
    return match.group(1) if match else None


def _token(page: str) -> str | None:
    match = _TOKEN_RE.search(page or "")
    return match.group(1) if match else None


def _url_from_json(text: str) -> str | None:
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("status") != "ok":
        return None
    return data.get("url") or None


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # One session for both steps: the token POST needs the
        # page's cookies.
        _final, page, status = await request(session, url)
        if status == 404:
            raise HostError(
                "krakenfiles: file not found — the link is dead or "
                "the file was removed")
        if status >= 400:
            raise HostError(f"krakenfiles answered HTTP {status}")
        action = _form_action(page)
        token = _token(page)
        if not action or not token:
            raise HostError(
                "couldn't find the download form on the krakenfiles "
                "page — the file may have been removed, or the site "
                "changed its layout")
        post_url = normalise_url(action, url)
        _final2, body, status = await request(
            session, post_url, method="POST", data={"token": token},
            headers={"Referer": url})
    if status >= 400:
        raise HostError(
            f"krakenfiles' download step answered HTTP {status}")
    media_url = _url_from_json(body)
    if not media_url:
        raise HostError(
            "krakenfiles didn't hand back a download link — try "
            "again in a bit")
    return {
        "title": page_title(page) or "krakenfiles file",
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
    name = safe_filename(info["title"], "krakenfiles file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
