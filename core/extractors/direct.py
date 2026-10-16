"""Engine 0 — the URL *is* a file.

A HEAD request tells us: if the response is not an HTML page, or the
server offers an attachment filename, we can stream the bytes straight
down with aiohttp instead of asking yt-dlp to understand a web page.

Playlists (.m3u8 HLS / .mpd DASH) are deliberately NOT files: they are
stream manifests, and downloading one would just save a few KB of text.
They return None here so the chain hands them to yt-dlp, which fetches
the actual video segments.
"""

import os
import re
from urllib.parse import unquote, urlparse

import aiohttp

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

FILE_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".mp3", ".m4a", ".flac",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".pdf", ".jpg", ".jpeg",
    ".png", ".webp", ".gif", ".apk", ".iso", ".bin",
}

#: Stream playlists — never "direct files", always handed to yt-dlp.
PLAYLIST_EXTS = {".m3u8", ".mpd"}


def is_playlist_url(url: str) -> bool:
    """True for HLS (.m3u8) / DASH (.mpd) manifest URLs."""
    return os.path.splitext(urlparse(url).path)[1].lower() in PLAYLIST_EXTS


#: Longest filename we will write (filesystems cap at 255 bytes).
_MAX_NAME_LEN = 200


def safe_filename(name: str, fallback: str = "file") -> str:
    """Tidy a filename: no path tricks, no control chars, bounded length."""
    name = os.path.basename((name or "").strip()) or fallback
    name = "".join(c for c in name if c.isprintable() or c in (" ",))
    name = name.strip() or fallback
    if len(name) > _MAX_NAME_LEN:
        root, ext = os.path.splitext(name)
        name = root[:_MAX_NAME_LEN - len(ext)] + ext
    return name


def filename_from(url: str, headers=None, fallback: str = "file") -> str:
    if headers:
        disposition = headers.get("Content-Disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
        if match:
            return safe_filename(unquote(match.group(1)), fallback)
    name = unquote(os.path.basename(urlparse(url).path)) or fallback
    return safe_filename(name, fallback)


async def check_direct(url: str) -> dict | None:
    """Return {filename, size, mime} if `url` looks like a raw file."""
    if is_playlist_url(url):
        return None  # a stream manifest — yt-dlp's job, not ours
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, allow_redirects=True,
                                    headers={"User-Agent": BROWSER_UA}) as resp:
                if resp.status >= 400:
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                disposition = resp.headers.get("Content-Disposition", "")
                if "text/html" in ctype and "attachment" not in disposition \
                        and ext not in FILE_EXTS:
                    return None  # a web page — let the other engines try
                return {
                    "filename": filename_from(url, resp.headers),
                    "size": int(resp.headers.get("Content-Length") or 0),
                    "mime": ctype.split(";")[0],
                    "final_url": str(resp.url),
                }
    except Exception:
        # Some hosts reject HEAD; if the path itself screams "file",
        # still give the direct engine a chance.
        if ext in FILE_EXTS:
            return {"filename": filename_from(url), "size": 0,
                    "mime": "", "final_url": url}
        return None


async def download_file(url: str, dest_dir: str, progress_cb=None,
                        filename: str | None = None, headers: dict | None = None,
                        cookies: dict | None = None) -> str:
    """Stream `url` to disk in chunks (never loads the file into RAM)."""
    req_headers = {"User-Agent": BROWSER_UA}
    if headers:
        req_headers.update(headers)
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=req_headers, cookies=cookies,
                               allow_redirects=True) as resp:
            resp.raise_for_status()
            name = filename or filename_from(url, resp.headers)
            path = os.path.join(dest_dir, name)
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        await progress_cb(done, total)
            return path
