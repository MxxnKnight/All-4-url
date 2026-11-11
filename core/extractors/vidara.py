"""Custom extractor: Vidara (+ the Streamix-style clones).

Why this exists: no yt-dlp extractor exists for this panel, and the
watch page is a client-rendered app — its HTML has no media URL at all.
The player asks the site's own JSON endpoint for the stream:

    POST https://<host>/api/stream
    {"filecode": "<code>", "device": "web"}          -> {"streaming_url": ...}

with ``Referer``/``Origin`` on the host itself (Streamix clones expose
the same thing at ``/ajax/stream``). The reply carries an HLS master
playlist that is minted for the requesting IP, so it plays from the
bot's side with no browser and no account.

Verified live on 2026-11-11: ``POST https://vidara.to/api/stream``
answers JSON (``{"error": "Video not found"}`` + HTTP 404 for a made-up
code), i.e. the endpoint and its payload shape are current. A real
stream could not be exercised here (no public file code was available),
so the HLS branch is the same, already-shipping yt-dlp path that
luluvido uses.
"""

import os
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import HostError, fetch_page, page_title

#: Domains that run this player (vidara's own API docs list the mirrors).
VIDARA_HOSTS = (
    "vidara.to", "www.vidara.to",
    "vidara.so", "www.vidara.so",
    "vidaraa.cc", "www.vidaraa.cc",
    "vidavaca.cc", "www.vidavaca.cc",
    "vidchampions.com", "www.vidchampions.com",
    "thebesthosterv.com", "www.thebesthosterv.com",
    "viewdara.com", "www.viewdara.com",
    "odysseusa.cc", "www.odysseusa.cc",
    "streamix.so", "www.streamix.so",
    "stmix.io", "www.stmix.io",
)

#: Streamix clones expose the same call under a different path.
_AJAX_HOSTS = ("streamix", "stmix")


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in VIDARA_HOSTS


def _file_code(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if not parts:
        raise HostError("that vidara link has no file code in it")
    code = parts[-1]
    for suffix in (".html", ".htm"):
        if code.lower().endswith(suffix):
            code = code[: -len(suffix)]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code or ""):
        raise HostError("that vidara link has no usable file code in it")
    return code


def _endpoint(scheme: str, host: str) -> str:
    if any(marker in host for marker in _AJAX_HOSTS):
        return f"{scheme}://{host}/ajax/stream"
    return f"{scheme}://{host}/api/stream"


async def _stream_json(url: str, code: str) -> dict:
    parsed = urlparse(url)
    host = parsed.netloc
    scheme = parsed.scheme or "https"
    origin = f"{scheme}://{host}"
    payload = {"filecode": code, "device": "web"}
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer": url,
        "Origin": origin,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    timeout = aiohttp.ClientTimeout(total=35)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_endpoint(scheme, host), json=payload,
                                    headers=headers) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text(errors="replace")
                    raise HostError(
                        f"vidara's stream endpoint answered with a web page "
                        f"(HTTP {status}), not JSON — the site may be down "
                        f"or behind a browser check")
    except HostError:
        raise
    except Exception as exc:
        raise HostError(f"couldn't ask vidara for the stream ({exc})") from exc
    if not isinstance(data, dict):
        raise HostError("vidara's stream endpoint returned an unexpected reply")
    stream = (data.get("streaming_url") or data.get("url")
              or (data.get("data") or {}).get("streaming_url")
              if isinstance(data.get("data"), dict) else data.get("streaming_url"))
    if not stream:
        reason = data.get("error") or data.get("msg") or f"HTTP {status}"
        raise HostError(f"vidara wouldn't play this file ({reason})")
    return {"stream": stream, "raw": data}


async def resolve(url: str) -> dict:
    code = _file_code(url)
    info = await _stream_json(url, code)
    stream = info["stream"]
    if stream.startswith("//"):
        stream = "https:" + stream
    elif stream.startswith("/"):
        parsed = urlparse(url)
        stream = f"{parsed.scheme or 'https'}://{parsed.netloc}{stream}"
    data = info["raw"]
    title = (data.get("title") or data.get("video_title")
             or (data.get("data") or {}).get("title")
             if isinstance(data.get("data"), dict) else data.get("title"))
    if not title:
        try:
            _, page = await fetch_page(url)
            title = page_title(page)
        except HostError:
            title = f"vidara {code}"
    is_playlist = stream.lower().split("?")[0].endswith(".m3u8")
    return {
        "title": title,
        "media_url": stream,
        "approx_size": 0,
        "is_playlist": is_playlist,
        "referer": url,
    }


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    info = await resolve(url)
    title = safe_filename(info["title"], "vidara video")
    media_url = info["media_url"]
    if info["is_playlist"]:
        from core.extractors.ytdlp_engine import download as ytdlp_download
        paths = await ytdlp_download(media_url, job_dir,
                                     progress_cb=progress_cb,
                                     headers={"Referer": url})
        media = [p for p in paths
                 if os.path.splitext(p)[1].lower() in
                 (".mp4", ".mkv", ".webm", ".ts")]
        if media:
            ext = os.path.splitext(media[0])[1]
            dest = os.path.join(job_dir, title + ext)
            if dest != media[0]:
                try:
                    os.rename(media[0], dest)
                    paths[paths.index(media[0])] = dest
                except OSError:
                    pass
        return paths
    ext = os.path.splitext(urlparse(media_url).path)[1].lower() or ".mp4"
    if ext not in (".mp4", ".mkv", ".webm", ".m4v", ".mov"):
        ext = ".mp4"
    return [await download_file(media_url, job_dir, progress_cb=progress_cb,
                                filename=title + ext,
                                headers={"Referer": url})]
