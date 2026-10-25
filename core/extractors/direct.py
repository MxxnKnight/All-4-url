"""Engine 0 — the URL *is* a file.

A HEAD request tells us: if the response is not an HTML page, or the
server offers an attachment filename, we can stream the bytes straight
down with aiohttp instead of asking yt-dlp to understand a web page.

Playlists (.m3u8 HLS / .mpd DASH) are deliberately NOT files: they are
stream manifests, and downloading one would just save a few KB of text.
They return None here so the chain hands them to yt-dlp, which fetches
the actual video segments.
"""

import asyncio
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from core import stats

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


def _query_disposition_filename(url: str) -> str | None:
    """Real filename hiding in a signed URL's query string.

    S3/R2 presigned links (hubcloud etc.) carry
    `response-content-disposition=attachment; filename="Movie.mkv"` as a
    query parameter — the storage service turns it into a real
    Content-Disposition header on GET, but the bot's HEAD probe never
    gets that far (see check_direct). Reading it here means the file
    keeps its proper name instead of the URL's hash-like last segment.
    """
    for value in parse_qs(urlparse(url).query).get(
            "response-content-disposition", []):
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", value)
        if match:
            return safe_filename(unquote(match.group(1)))
    return None


def filename_from(url: str, headers=None, fallback: str = "file") -> str:
    if headers:
        disposition = headers.get("Content-Disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
        if match:
            return safe_filename(unquote(match.group(1)), fallback)
    query_name = _query_disposition_filename(url)
    if query_name:
        return query_name
    name = unquote(os.path.basename(urlparse(url).path)) or fallback
    return safe_filename(name, fallback)


async def check_direct(url: str) -> dict | None:
    """Return {filename, size, mime} if `url` looks like a raw file."""
    if is_playlist_url(url):
        return None  # a stream manifest — yt-dlp's job, not ours
    # Signed storage links: the signature covers GET only, so the HEAD
    # probe below would be rejected (403) and the link would fall
    # through to yt-dlp, which then chokes on the query string as a
    # filename. A response-content-disposition filename in the query
    # already proves this is a file download — trust it, no probe
    # needed. The direct flow's GET is the signed method, so it works.
    query_name = _query_disposition_filename(url)
    if query_name:
        return {"filename": query_name, "size": 0, "mime": "",
                "final_url": url}
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
            stats.record_paths("downloaded", [path])
            return path


#: Parallel-download tuning. Only used for hosts proven to support
#: Range (FileSnag's backend does — verified 2026-10-25: concurrent
#: Range requests all returned 206 with byte-exact slices). Small
#: files are not worth the extra connections, so below this size the
#: caller falls back to the single-stream download_file above.
PARALLEL_MIN_BYTES = 20 * 1024 * 1024
PARALLEL_CONNECTIONS = 4


async def download_file_parallel(url: str, dest_dir: str, progress_cb=None,
                                 filename: str | None = None,
                                 headers: dict | None = None,
                                 cookies: dict | None = None) -> str:
    """Download in parallel Range chunks, falling back to single-stream.

    Why this exists: FileSnag's download server (the host behind its
    `fsn_media` link — a 302 to its own backend, NOT a WordPress proxy
    stream, and NOT a Terabox CDN link; the page JS only ever hands
    out the fsn_media URL, there is no hidden Terabox link to steal)
    throttles a single connection, but serves concurrent Range
    requests fine. So: probe with `Range: bytes=0-0`, follow the
    redirect once to the real backend URL, then fetch N chunks
    concurrently from that URL and stitch them on disk.

    Safety: chunks are streamed to disk at their own offsets (never
    held in RAM); every chunk's byte count is verified; ANY failure,
    a 200 instead of 206, a missing size, or a host without
    Accept-Ranges, discards the partial file and retries the whole
    download single-stream via download_file. A cancelled job is
    never retried — cancellation re-raises.
    """
    req_headers = {"User-Agent": BROWSER_UA}
    if headers:
        req_headers.update(headers)
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
    path = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Probe: 1 byte, following the redirect to the backend.
            async with session.get(
                    url, headers={**req_headers, "Range": "bytes=0-0"},
                    cookies=cookies, allow_redirects=True) as resp:
                if resp.status != 206:
                    raise ValueError(
                        f"Range probe returned {resp.status}, not 206")
                content_range = resp.headers.get("Content-Range", "")
                if resp.headers.get("Accept-Ranges", "").lower() != "bytes":
                    raise ValueError("host does not advertise Accept-Ranges")
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except (IndexError, ValueError):
                    raise ValueError("Range probe gave no usable total")
                if total < PARALLEL_MIN_BYTES:
                    raise ValueError("file too small for parallel download")
                await resp.read()  # 1 byte — release the connection
                final_url = str(resp.url)
                name = filename or filename_from(final_url, resp.headers)
            path = os.path.join(dest_dir, name)
            with open(path, "wb") as fh:
                fh.truncate(total)
            chunk_size = (total + PARALLEL_CONNECTIONS - 1) \
                // PARALLEL_CONNECTIONS
            ranges = [(i * chunk_size, min(total - 1, (i + 1) * chunk_size - 1))
                      for i in range(PARALLEL_CONNECTIONS)]
            done = [0]

            async def _fetch(start: int, end: int) -> None:
                written = 0
                async with session.get(
                        final_url,
                        headers={**req_headers,
                                 "Range": f"bytes={start}-{end}"},
                        cookies=cookies, allow_redirects=True) as resp:
                    if resp.status != 206:
                        raise ValueError(
                            f"chunk {start}-{end} returned {resp.status}")
                    with open(path, "r+b") as fh:
                        fh.seek(start)
                        async for piece in resp.content.iter_chunked(
                                256 * 1024):
                            if written + len(piece) > end - start + 1:
                                raise ValueError(
                                    f"chunk {start}-{end} overran its range")
                            fh.write(piece)
                            written += len(piece)
                            done[0] += len(piece)
                            if progress_cb:
                                await progress_cb(done[0], total)
                if written != end - start + 1:
                    raise ValueError(
                        f"chunk {start}-{end} short: {written} bytes")

            await asyncio.gather(*[_fetch(s, e) for s, e in ranges])
            if done[0] != total:
                raise ValueError(f"parallel total {done[0]} != {total}")
    except asyncio.CancelledError:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    except Exception:
        # Any doubt at all: throw the partial away and do it the
        # slow, proven way. A correct slow download beats a fast
        # corrupt one, and the caller never sees the difference
        # except in speed.
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        return await download_file(url, dest_dir, progress_cb=progress_cb,
                                   filename=filename, headers=headers,
                                   cookies=cookies)
    stats.record_paths("downloaded", [path])
    return path
