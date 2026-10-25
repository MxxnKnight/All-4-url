"""Engine 1 — yt-dlp for the 1000+ video/audio sites it understands.

yt-dlp's Python API is blocking, so every call runs in a thread-pool
executor. Progress hooks fire in that worker thread; they only update a
small shared dict, and an async poller turns it into throttled edits.
"""

import asyncio
import os

import config
from core import stats
from core.cookies import jar_for_url
from core.utils import VIDEO_EXTS, list_files

#: Audio extensions, for ordering downloads video/audio-first.
_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".opus", ".ogg"}


class YtdlpError(Exception):
    pass


def _base_opts(dest_dir: str, url: str | None = None,
               state: dict | None = None) -> dict:
    opts = {
        # NOTE: title was capped at 70 but id was NOT — and for a
        # generic (non-site) link yt-dlp sets id to the URL itself.
        # A signed storage URL's query string then blew the filename
        # past Linux's 255-byte limit (Errno 36, crashed a job on
        # 2026-10-25). Cap id too: worst case here is ~70+2+40+1+ext,
        # safely under the limit. Job dirs are per-job, so truncating
        # the id can't collide two files.
        "outtmpl": os.path.join(dest_dir, "%(title).70s [%(id).40s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    cookies = jar_for_url(url) if url else config.cookies_path()
    if cookies:
        opts["cookiefile"] = cookies
    if state is not None:
        def hook(d):
            state.update(d)
        opts["progress_hooks"] = [hook]
    return opts


def _extract_sync(url: str, opts: dict) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    try:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise YtdlpError(str(exc)[:500]) from exc


async def extract_info(url: str, ignore_no_formats: bool = False) -> dict:
    """Metadata only. `ignore_no_formats` matters for Instagram image
    posts: yt-dlp then returns the extracted entries (with CDN image
    URLs) instead of erroring out with 'no video formats found'."""
    opts = _base_opts(config.DOWNLOAD_DIR, url)
    opts.pop("outtmpl", None)
    if ignore_no_formats:
        opts["ignore_no_formats_error"] = True
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_sync, url, opts)


def carousel_image_urls(info: dict) -> list[str]:
    """Pull direct image URLs out of an extract_info result, if any."""
    urls: list[str] = []

    def visit(node):
        if not isinstance(node, dict):
            return
        if node.get("_type") in ("url", "url_transparent"):
            return
        entries = node.get("entries")
        if entries:
            for entry in entries:
                visit(entry)
            return
        url = node.get("url") or ""
        ext = (node.get("ext") or "").lower()
        if url.startswith("http") and ext in ("jpg", "jpeg", "png", "webp"):
            urls.append(url)
        for thumb in node.get("thumbnails") or []:
            pass  # thumbnails are lower quality; entry urls are preferred

    visit(info)
    # De-dupe, keep order.
    return list(dict.fromkeys(urls))


def best_formats(info: dict) -> list[dict]:
    """A short, button-friendly view of downloadable formats."""
    formats = []
    for fmt in info.get("formats") or []:
        height = fmt.get("height")
        if not height:
            continue
        formats.append({
            "format_id": fmt.get("format_id"),
            "height": height,
            "ext": fmt.get("ext"),
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
        })
    formats.sort(key=lambda f: f["height"], reverse=True)
    # Keep the best entry per height.
    seen, unique = set(), []
    for fmt in formats:
        if fmt["height"] in seen:
            continue
        seen.add(fmt["height"])
        unique.append(fmt)
    return unique


async def download(url: str, dest_dir: str, format_id: str | None = None,
                   audio_only: bool = False, progress_cb=None) -> list[str]:
    """Download and return the list of new file paths in dest_dir."""
    state: dict = {}
    opts = _base_opts(dest_dir, url, state)
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif format_id:
        opts["format"] = f"{format_id}+bestaudio/{format_id}/best"
    else:
        opts["format"] = "bestvideo+bestaudio/best"

    if not audio_only:
        # Subtitles ride along with the video: yt-dlp writes them next
        # to it (Malayalam + English, SRT), and the sender delivers
        # them right after the video. Only subtitles the uploader
        # provided — never auto-generated captions. When a video has
        # no subtitles, nothing extra appears; the job is unchanged.
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = False
        opts["subtitleslangs"] = ["ml", "en.*"]
        opts["subtitlesformat"] = "srt/best"

    before = set(list_files(dest_dir))
    stop_polling = asyncio.Event()

    async def poll():
        last = -1
        while not stop_polling.is_set():
            await asyncio.sleep(2)
            if progress_cb and state.get("status") == "downloading":
                done = state.get("downloaded_bytes") or 0
                total = state.get("total_bytes") or state.get("total_bytes_estimate") or 0
                if done != last:
                    last = done
                    await progress_cb(done, total)

    def _download_sync():
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
        except DownloadError as exc:
            raise YtdlpError(str(exc)[:500]) from exc

    poll_task = asyncio.create_task(poll())
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _download_sync)
    finally:
        stop_polling.set()
        poll_task.cancel()

    new_files = [p for p in list_files(dest_dir) if p not in before]
    if not new_files:
        raise YtdlpError("yt-dlp finished but produced no file")
    # The sender gives the caption to the first file only, so the
    # media itself must come first: a ".en.srt" would otherwise sort
    # ahead of the ".mp4" and steal the video's caption.
    _media = VIDEO_EXTS | _AUDIO_EXTS
    new_files.sort(key=lambda p: (
        0 if os.path.splitext(p)[1].lower() in _media else 1, p))
    stats.record_paths("downloaded", new_files)
    return new_files
