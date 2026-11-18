"""Custom extractor: lulacloud.com file links.

Why this exists: yt-dlp has no lulacloud extractor, and the page is
just a landing — the file comes from POSTing the page's own URL and
reading the redirect it answers with. No form, no player, nothing
the generic gate walker can drive.

How it works:
  1. POST the file URL itself (empty body, the page as Referer).
  2. The reply's Location header is the file's URL.
  3. Download it directly.

Not verified live: the site does not answer from this sandbox at
all (empty reply), so the flow is replay-tested against the
documented shape only. The first real link from the owner is the
live proof.
"""

from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import HostError


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("lulacloud.com", "www.lulacloud.com"):
        return False
    return bool(urlparse(url).path.strip("/"))


async def _post_location(url: str) -> str | None:
    """POST the URL with no body; returns the Location or None."""
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                    url, allow_redirects=False,
                    headers={"User-Agent": BROWSER_UA,
                             "Referer": url}) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    return resp.headers.get("Location")
                return None
    except Exception:
        return None


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    direct = await _post_location(url)
    if not direct:
        raise HostError(
            "lulacloud gave no download link for that file — it may "
            "have been removed, or the site changed how it answers")
    name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return {
        "title": name or "lulacloud file",
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
    name = safe_filename(info["title"], "lulacloud file")
    ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
    if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
        name += "." + ext
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name, headers={"Referer": url})]
