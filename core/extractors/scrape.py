"""Engine 3 (last resort) — scrape the page itself.

Many small sites embed the real media URL in OpenGraph / Twitter Card
meta tags or a plain <video> tag. If yt-dlp and gallery-dl both failed,
fetching the HTML and regexing those well-known spots still rescues a
surprising number of links.
"""

import html as html_lib
import re

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file

PATTERNS = (
    r'property=["\']og:video(?::secure_url)?["\']\s+content=["\']([^"\']+)',
    r'name=["\']twitter:player:stream["\']\s+content=["\']([^"\']+)',
    r'property=["\']og:image["\']\s+content=["\']([^"\']+)',
    r'<video[^>]+src=["\']([^"\']+)',
    r'(https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?)',
    r'(https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?)',
)


class ScrapeError(Exception):
    pass


async def find_media_url(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": BROWSER_UA}) as resp:
            if resp.status >= 400:
                raise ScrapeError(f"page returned HTTP {resp.status}")
            page = await resp.text(errors="replace")
    for pattern in PATTERNS:
        match = re.search(pattern, page, re.I)
        if match:
            found = html_lib.unescape(match.group(1))
            if found.startswith("//"):
                found = "https:" + found
            return found
    raise ScrapeError("no embedded media URL found in the page")


async def download(url: str, dest_dir: str, progress_cb=None) -> str:
    media_url = await find_media_url(url)
    return await download_file(media_url, dest_dir, progress_cb=progress_cb,
                               headers={"Referer": url})
