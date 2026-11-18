"""Custom extractor: 1fichier.com file links.

Why this exists: yt-dlp has no 1fichier extractor, and the file
page is a plain form page — POSTing the link itself answers with
the download button (<a class="ok btn-general btn-orange">).
1fichier links look like https://1fichier.com/?<code>.

How it works:
  1. POST the file URL with no body (the site answers the POST
     with the download page).
  2. Read the orange download button's href.
  3. Download it directly.
  4. The site's own limits fail honestly: a "you must wait N
     minutes" page (free-download cooldown, common from datacenter
     IPs) says how long to wait, and a password-protected file
     says it needs a password instead of guessing.

Not verified live: only the homepage was reachable from this
sandbox, so the flow is replay-tested against the documented page
shape only. The first real link from the owner is the live proof.
"""

import re
from urllib.parse import urlparse

from core.extractors.direct import download_file, safe_filename
from core.extractors.player_scan import (HostError, fetch, page_title)

#: <a class="ok btn-general btn-orange" href="https://...">
_BUTTON_RE = re.compile(
    r"""<a[^>]*class=["']ok btn-general btn-orange["'][^>]*href=["']([^"']+)["']""",
    re.I)
#: The site's warning boxes, which carry the wait/password text.
_WARN_RE = re.compile(
    r"""<div[^>]*class=["']ct_warn["'][^>]*>(.*?)</div>""", re.S | re.I)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("1fichier.com", "www.1fichier.com"):
        return False
    # A 1fichier file link is always /?<code> — the bare homepage
    # has no code and is not a file.
    return bool(urlparse(url).query)


def _button_url(page: str) -> str | None:
    match = _BUTTON_RE.search(page or "")
    return match.group(1).replace("&amp;", "&") if match else None


def _limit_message(page: str) -> str | None:
    """The site's own wait/password message, if it shows one."""
    warnings = _WARN_RE.findall(page or "")
    if not warnings:
        return None
    text = " ".join(
        re.sub(r"<[^>]+>", " ", w) for w in warnings).lower()
    text = re.sub(r"\s+", " ", text).strip()
    if "you must wait" in text:
        numbers = [w for w in text.split() if w.isdigit()]
        if numbers:
            return ("1fichier says to wait "
                    f"{numbers[0]} minute(s) before the next free "
                    "download — try again later")
        return ("1fichier says to wait before the next free "
                "download — try again later")
    if "protect access" in text or "password" in text:
        return ("this 1fichier file needs a password to download, "
                "and the bot has no way to ask for one")
    return None


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    _final, page, status = await fetch(url, method="POST")
    if status == 404:
        raise HostError(
            "1fichier: file not found — the link is dead or the "
            "file was removed")
    if status >= 400:
        raise HostError(f"1fichier answered HTTP {status}")
    media_url = _button_url(page)
    if media_url:
        return {
            "title": page_title(page) or "1fichier file",
            "media_url": media_url,
            "approx_size": 0,
            "is_playlist": False,
        }
    limit = _limit_message(page)
    if limit:
        raise HostError(limit)
    raise HostError(
        "couldn't find the download button on the 1fichier page — "
        "the file may have been removed, or the site changed its "
        "layout")


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    # cancel_check is accepted for the extractor interface but
    # unused here: this module downloads directly, and a direct
    # download aborts when progress_cb raises JobCancelled.
    """Resolve the link and download the file; returns file paths."""
    info = await resolve(url)
    name = safe_filename(info["title"], "1fichier file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
