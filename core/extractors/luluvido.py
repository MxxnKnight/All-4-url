"""Custom extractor: luluvido.com (a LuluStream file host).

Why this exists: yt-dlp has NO extractor for this site (latest
yt-dlp, tested 2026-10-26: "Unsupported URL" on a luluvido page),
and the bot's other engines find nothing either — the page's
static HTML has no <video> tag, no download form, and no direct
link. The video only appears after JavaScript runs: the page
ships a Dean Edwards packed eval() block that, once unpacked,
contains a jwplayer setup with a single HLS master-playlist
source. Unpacking that block in plain Python is all the bot
needs — no browser, which could never run on Koyeb free anyway.

How it works, in order:
  1. GET the page (plain HTTP, browser UA — the site does not
     Cloudflare-block a plain request).
  2. Find the packed eval() block, unpack it (base-36 dictionary
     substitution, the same algorithm the packer uses).
  3. Pull the jwplayer source file out of the unpacked code.
     That URL is generated for THIS request, from THIS server —
     so unlike a 1DM-extracted link (locked to the phone's IP),
     a URL the bot extracts itself works from the bot's IP.
  4. If it is an HLS master playlist, estimate the size from its
     highest BANDWIDTH x the setup duration, so an over-2 GB
     video is refused before downloading, not after.
  5. HLS -> yt-dlp downloads the segments and muxes to mp4;
     a direct mp4 -> the direct engine streams it down.

Fragility, stated plainly: this extractor reads the site's
player code, so if luluvido changes its player (different
packer, different player, a captcha in front of the page),
this breaks and the bot will say so instead of silently
sending a wrong file. That is the price of a custom
extractor, and it is why the registry below keeps each site
isolated: one site breaking must never break the others.

Future sites: add a module like this one (is_site_url,
resolve, download) and register it in
core/extractors/site_registry.py — one line, no changes to
the uploader's resolver chain.
"""

import asyncio
import os
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.utils import human_bytes

BASE = "https://luluvido.com"


class LuluvidoError(Exception):
    pass


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in ("luluvido.com", "www.luluvido.com")


def _file_code(url: str) -> str:
    code = urlparse(url).path.strip("/").split("/")[0]
    if not code or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        raise LuluvidoError("that luluvido link has no file code in it")
    return code


# ---------------------------------------------------------------- packer

_PACKER_RE = re.compile(
    r"\}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\)", re.S)
_BASE_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _base_n(n: int, base: int) -> str:
    out = ""
    while True:
        out = _BASE_CHARS[n % base] + out
        n //= base
        if n == 0:
            return out


def unpack_packed(js: str) -> str | None:
    """Unpack a Dean Edwards packed eval() block, or None."""
    match = _PACKER_RE.search(js)
    if not match:
        return None
    payload, base, count = match.group(1), int(match.group(2)), int(match.group(3))
    words = match.group(4).split("|")
    out = payload
    for i in range(count - 1, -1, -1):
        if i < len(words) and words[i]:
            out = re.sub(r"\b" + _base_n(i, base) + r"\b", words[i], out)
    return out


def _media_from_js(js: str) -> tuple[str, float] | None:
    """(media_url, duration_seconds) from jwplayer setup code."""
    match = re.search(
        r"sources:\s*\[\{file:\s*\"([^\"]+)\"", js)
    if not match:
        return None
    duration = 0.0
    dur = re.search(r"duration:\s*\"([\d.]+)\"", js)
    if dur:
        try:
            duration = float(dur.group(1))
        except ValueError:
            duration = 0.0
    return match.group(1), duration


def _title_from_html(html: str) -> str:
    match = re.search(r"<title>([^<]+)</title>", html, re.I)
    if not match:
        return "luluvido video"
    title = match.group(1).strip()
    title = re.sub(r"\s*-\s*Lulustream(\.mp4)?\s*$", "", title, flags=re.I)
    return title or "luluvido video"


async def _fetch_page(code: str) -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{BASE}/{code}",
                                   headers={"User-Agent": BROWSER_UA}) as resp:
                if resp.status >= 400:
                    raise LuluvidoError(
                        f"luluvido page returned {resp.status} — "
                        f"the link may be dead or removed")
                return await resp.text(errors="ignore")
    except LuluvidoError:
        raise
    except Exception as exc:
        raise LuluvidoError(f"couldn't load the luluvido page ({exc})")


async def _approx_size(media_url: str, duration: float) -> int:
    """Best-effort size for an HLS video: max BANDWIDTH x duration.

    Returns 0 when it cannot be estimated (direct mp4 size comes
    from its own HEAD probe in the caller instead).
    """
    if not media_url.lower().split("?")[0].endswith(".m3u8") or not duration:
        return 0
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(media_url,
                                   headers={"User-Agent": BROWSER_UA}) as resp:
                if resp.status >= 400:
                    return 0
                body = await resp.text(errors="ignore")
        bandwidths = [int(b) for b in
                      re.findall(r"BANDWIDTH=(\d+)", body)]
        if not bandwidths:
            return 0
        return int(max(bandwidths) * duration / 8)
    except Exception:
        return 0


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    code = _file_code(url)
    html = await _fetch_page(code)
    title = _title_from_html(html)
    found = None
    # Try every script body: the packed block's position on the
    # page is not part of the contract, only its shape is.
    for body in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S):
        unpacked = unpack_packed(body)
        if unpacked:
            found = _media_from_js(unpacked)
            if found:
                break
    if not found:
        # Unpacked-player sites sometimes ship the setup plain.
        found = _media_from_js(html)
    if not found:
        raise LuluvidoError(
            "couldn't find the video in the luluvido page — the "
            "site may have changed its player")
    media_url, duration = found
    if media_url.startswith("/"):
        media_url = BASE + media_url
    is_playlist = media_url.lower().split("?")[0].endswith(".m3u8")
    approx = await _approx_size(media_url, duration) if is_playlist else 0
    return {"title": title, "media_url": media_url,
            "approx_size": approx, "is_playlist": is_playlist,
            "duration": duration}


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    """Resolve the page and download the video; returns file paths."""
    info = await resolve(url)
    title = safe_filename(info["title"], "luluvido video")
    media_url = info["media_url"]
    if info["is_playlist"]:
        from core.extractors.ytdlp_engine import download as ytdlp_download
        paths = await ytdlp_download(media_url, job_dir,
                                     progress_cb=progress_cb)
        # Give the file the page's title instead of yt-dlp's
        # playlist-derived name (subtitles, if any, keep theirs).
        media = [p for p in paths
                 if os.path.splitext(p)[1].lower() in (".mp4", ".mkv", ".webm")]
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
    ext = os.path.splitext(media_url.split("?")[0])[1].lower() or ".mp4"
    return [await download_file(media_url, job_dir, progress_cb=progress_cb,
                                filename=title + ext)]
