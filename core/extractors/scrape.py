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
from core.extractors.direct import BROWSER_UA, download_file, is_playlist_url
from core.utils import run_blocking

PATTERNS = (
    r'property=["\']og:video(?::secure_url)?["\']\s+content=["\']([^"\']+)',
    r'name=["\']twitter:player:stream["\']\s+content=["\']([^"\']+)',
    r'<video[^>]+src=["\']([^"\']+)',
    r'(https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?)',
    r'(https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?)',
    # og:image LAST: a page whose only video is behind JavaScript
    # (click-to-play players) still carries a thumbnail, and the
    # thumbnail must never beat a real video URL in the same HTML.
    # An image-only result does not stop the chain either — the
    # scrape flows give the browser a turn first (see
    # plugins/uploader._scrape_flow and core/sendlink).
    r'property=["\']og:image["\']\s+content=["\']([^"\']+)',
)

#: Extensions that mark a URL as an image file.
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")


def is_image_url(url: str) -> bool:
    """True when `url` points at an image file (by extension)."""
    from urllib.parse import urlparse
    return os.path.splitext(urlparse(url or "").path)[1].lower() \
        in IMAGE_EXTS


class ScrapeError(Exception):
    pass


def _sanitize(text: str) -> str:
    """Strip URLs from an error line before it reaches the user."""
    return re.sub(r"https?://[^\s'\"<>]+", "[source]", text or "")


#: Marker of the "cached placeholder" player shell some hosts serve to
#: every visitor. The real player (and its media URL) lives only on
#: the uncached page, which the site's own script loads as ?nc=1.
_PLACEHOLDER_MARKER = "xvs-ph"


async def _fetch_html(session, page_url: str, referer: str | None = None) -> str:
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    async with session.get(page_url, headers=headers) as resp:
        if resp.status >= 400:
            raise ScrapeError(f"page returned HTTP {resp.status}")
        return await resp.text(errors="replace")


def _static_candidates(page: str, page_url: str) -> list[str]:
    """Media URLs the page's own markup/config claims, best first.

    One hit per PATTERN (their order is the confidence order), then
    the deeper player_scan pass (JSON configs, packed scripts,
    srcdoc frames).
    """
    found: list[str] = []
    for pattern in PATTERNS:
        match = re.search(pattern, page, re.I)
        if not match:
            continue
        # Make it absolute before anything probes or returns it: a
        # root-relative "<video src=\"/fake.mp4\">" used to be handed
        # to the downloader as-is, and the probe could not tell a
        # relative URL was not a file.
        candidate = player_scan.normalise_url(
            html_lib.unescape(match.group(1)), page_url)
        if candidate not in found:
            found.append(candidate)
    deeper = player_scan.find_media(page, page_url)
    if deeper and deeper not in found:
        found.append(deeper)
    return found


async def _is_real_media(session, candidate: str, referer: str) -> bool:
    """Does this URL really return bytes, or just an HTML page?

    A page can contain a URL that only looks like a video (an
    `/e/<uuid>/video.mp4` route that answers HTML — proven on a real
    firestream.to page, 2026-11-18). Saving that would hand the owner
    a "video" that is a web page. One 2-byte ranged request settles
    it. When the probe itself fails (timeout, host refuses Range)
    the candidate is kept: an unproven link is better than none.
    """
    headers = {"User-Agent": BROWSER_UA, "Range": "bytes=0-1"}
    if referer:
        headers["Referer"] = referer
    try:
        async with session.get(candidate, headers=headers,
                               allow_redirects=True) as resp:
            if resp.status >= 400:
                return False
            ctype = (resp.headers.get("Content-Type") or "").lower()
            disposition = (resp.headers.get("Content-Disposition") or "").lower()
            if "text/html" in ctype and "attachment" not in disposition:
                return False
            return True
    except Exception:
        return True


async def find_media_url(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        page = await _fetch_html(session, url)
        pages = [page]
        # Cached placeholder shell: the real player is on the uncached
        # page (?nc=1), so read that one first.
        if _PLACEHOLDER_MARKER in page.lower() and "nc=1" not in url:
            sep = "&" if "?" in url else "?"
            try:
                pages.insert(0, await _fetch_html(session,
                                                  f"{url}{sep}nc=1"))
            except ScrapeError:
                pass
        image_fallback = None
        for text in pages:
            for candidate in _static_candidates(text, url):
                if is_image_url(candidate):
                    # A thumbnail is a fallback, never the first answer:
                    # the page may still hide a real video (the caller
                    # gives the browser a turn on an image result).
                    image_fallback = image_fallback or candidate
                    continue
                if is_playlist_url(candidate):
                    return candidate
                if await _is_real_media(session, candidate, url):
                    return candidate
        # The player is often a separate document behind an <iframe>.
        # Each frame fetch gets its own short timeout so one slow ad
        # frame cannot eat the whole budget.
        for text in pages:
            for src in player_scan.iframe_srcs(text, url):
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
        if image_fallback:
            return image_fallback
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
