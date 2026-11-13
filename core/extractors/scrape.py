"""Engine 3 (last resort) — scrape the page itself.

Many small sites embed the real media URL in OpenGraph / Twitter Card
meta tags or a plain <video> tag. If yt-dlp and gallery-dl both failed,
fetching the HTML and regexing those well-known spots still rescues a
surprising number of links.

When the page itself holds nothing, two more static tricks (borrowed
from how the Ghost Downloader extension finds media, minus the browser
it runs in) get a turn before giving up:

  * the player config scan in player_scan (nested JSON, packed scripts),
  * the player <iframe>: the video link often lives in the framed
    document, not the page that embeds it,
  * a playlist hidden in a data: URI, decoded and handed to ffmpeg.
"""

import html as html_lib
import os
import re
import subprocess

import aiohttp

from core.extractors import player_scan
from core.extractors.direct import BROWSER_UA, download_file
from core.utils import run_blocking

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


def _sanitize(text: str) -> str:
    """Strip URLs from an error line before it reaches the user."""
    return re.sub(r"https?://[^\s'\"<>]+", "[source]", text or "")


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
        found = player_scan.find_media(page, url)
        if found:
            return found
        # The player is often a separate document behind an <iframe>.
        # Each frame fetch gets its own short timeout so one slow ad
        # frame cannot eat the whole budget.
        for src in player_scan.iframe_srcs(page, url):
            try:
                req_timeout = aiohttp.ClientTimeout(total=15)
                async with session.get(
                        src, headers={"User-Agent": BROWSER_UA,
                                      "Referer": url},
                        timeout=req_timeout) as resp:
                    if resp.status >= 400:
                        continue
                    iframe_page = await resp.text(errors="replace")
            except Exception:
                continue
            found = player_scan.find_media(iframe_page, src)
            if found:
                return found
    raise ScrapeError("no embedded media URL found in the page")


async def _fetch_page_text(url: str) -> str | None:
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url,
                                   headers={"User-Agent": BROWSER_UA}) as resp:
                if resp.status >= 400:
                    return None
                return await resp.text(errors="replace")
    except Exception:
        return None


def _run_ffmpeg(cmd: list[str]) -> tuple[int, str]:
    """Blocking ffmpeg run (called through run_blocking)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=600)
        err = [line for line in (proc.stderr or "").strip().splitlines()
               if line.strip()]
        return proc.returncode, " / ".join(err[-3:]) if err else ""
    except subprocess.TimeoutExpired:
        return -1, "timed out after 10 minutes"


async def download_playlist(url: str, dest_dir: str,
                            progress_cb=None) -> str:
    """Download a video whose playlist hides in a data: URI on the page.

    Returns the finished .mp4 path. Raises ScrapeError when the page
    holds no data: playlist or ffmpeg cannot fetch its segments.
    """
    page = await _fetch_page_text(url)
    if page is None:
        raise ScrapeError("couldn't fetch the page")
    text = player_scan.extract_data_playlist(page)
    if not text:
        raise ScrapeError("no embedded playlist found in the page")
    text = player_scan.resolve_playlist_urls(text, url)
    playlist_path = os.path.join(dest_dir, "playlist.m3u8")
    out_path = os.path.join(dest_dir, "video.mp4")
    with open(playlist_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        # No -headers/-referer here: ffmpeg rejects http options when
        # the playlist input is a local file ("Option headers not
        # found"), and the segments are fetched by the HLS demuxer,
        # not the input protocol. Tokenised segment URLs carry their
        # own auth, so this works for the common case.
        # -protocol_whitelist is required: a local playlist may only
        # reference file:// segments by default, and ours are http(s).
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-protocol_whitelist",
               "file,http,https,tcp,tls,crypto,data",
               "-i", playlist_path, "-c", "copy",
               "-movflags", "+faststart", "-y", out_path]
        code, last_err = await run_blocking(_run_ffmpeg, cmd)
        if code != 0:
            raise ScrapeError("ffmpeg couldn't fetch the playlist"
                              + (f" ({_sanitize(last_err)})" if last_err
                                 else ""))
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise ScrapeError("ffmpeg produced no video file")
        if progress_cb is not None:
            size = os.path.getsize(out_path)
            await progress_cb(size, size)
        return out_path
    finally:
        try:
            os.unlink(playlist_path)
        except OSError:
            pass


async def download(url: str, dest_dir: str, progress_cb=None) -> str:
    try:
        media_url = await find_media_url(url)
    except ScrapeError:
        return await download_playlist(url, dest_dir, progress_cb=progress_cb)
    return await download_file(media_url, dest_dir, progress_cb=progress_cb,
                               headers={"Referer": url})
