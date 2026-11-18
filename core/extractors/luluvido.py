"""Custom extractor: LuluStream and its clone panels.

Why this exists: yt-dlp has NO extractor for these hosts (latest
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

Clone panels: the same LuluStream code runs on several domains
(luluvido.com, lulustream.com, lplaygo.com) with the same packed
jwplayer — and the "watch" URL shape differs per panel
(`/<code>`, `/d/<code>`, `/e/<code>`). So the extractor reads the
host from the link itself and, when the given page carries no
player, tries the panel's `/e/<code>` and `/<code>` pages before
giving up. Verified on a real lplaygo.com link (2026-11-18): the
`/d/` page is a 13 KB shell, its `/e/` iframe carries the packed
jwplayer, and the unpacked master.m3u8 answers 200 #EXTM3U.

Fragility, stated plainly: this extractor reads the site's
player code, so if the panel changes its player (different
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

import os
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import HostError, normalise_url

#: Panels known to run this player.
HOSTS = (
    "luluvido.com", "www.luluvido.com",
    "lulustream.com", "www.lulustream.com",
    "lplaygo.com", "www.lplaygo.com",
)
# luluvid.com deliberately absent: its DNS does not resolve (checked
# 2026-11-18), and filehosts already claims that spelling.

#: Page shapes the player hides behind, tried in order.
_PAGE_SHAPES = ("{url}", "{origin}/e/{code}", "{origin}/d/{code}",
                "{origin}/{code}")


def is_site_url(url: str) -> bool:
    return (urlparse(url).netloc or "").lower() in HOSTS


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _file_code(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if not parts:
        raise HostError("that lulustream link has no file code in it")
    code = parts[-1]
    for suffix in (".html", ".htm"):
        if code.lower().endswith(suffix):
            code = code[: -len(suffix)]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code or ""):
        raise HostError("that lulustream link has no usable file code in it")
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


_FILE_RE = re.compile(r"""file\s*:\s*["']([^"']+)["']""")
_DURATION_RE = re.compile(r"""duration\s*:\s*["']?([\d.]+)["']?""")


def _media_from_js(js: str) -> tuple[str, float] | None:
    """(media_url, duration_seconds) from jwplayer setup code."""
    match = _FILE_RE.search(js or "")
    if not match:
        return None
    media = match.group(1)
    # The first `file:` in a packed player is the source; a later
    # one is the logo/track. Only accept something playable.
    if not re.search(r"\.(m3u8|mp4|mkv|webm|mpd)(\?|$)", media, re.I):
        return None
    duration = 0.0
    dur = _DURATION_RE.search(js)
    if dur:
        try:
            duration = float(dur.group(1))
        except ValueError:
            duration = 0.0
    return media, duration


def _title_from_html(html: str) -> str:
    match = re.search(r"<title>([^<]+)</title>", html or "", re.I)
    if not match:
        return "lulustream video"
    title = match.group(1).strip()
    title = re.sub(r"\s*-\s*Lulustream(\.mp4)?\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*-\s*LuluStream\s*$", "", title, flags=re.I)
    return title or "lulustream video"


async def _fetch_page(page_url: str) -> tuple[str, str]:
    """GET a panel page; returns (final_url, html).

    The final URL matters: panels redirect between their mirror
    domains, and a relative media path must be resolved against the
    page that actually served it.
    """
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(page_url,
                                   headers={"User-Agent": BROWSER_UA}) as resp:
                if resp.status >= 400:
                    raise HostError(
                        f"the lulustream page returned {resp.status} — "
                        f"the link may be dead or removed")
                return str(resp.url), await resp.text(errors="ignore")
    except HostError:
        raise
    except Exception as exc:
        raise HostError(f"couldn't load the lulustream page ({exc})") from exc


def _find_in_page(html: str) -> tuple[str, float] | None:
    """The jwplayer source hidden in the page's scripts (packed or plain)."""
    for body in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S):
        unpacked = unpack_packed(body)
        if unpacked:
            found = _media_from_js(unpacked)
            if found:
                return found
    return _media_from_js(html)


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
        bandwidths = [int(b) for b in re.findall(r"BANDWIDTH=(\d+)", body)]
        if not bandwidths:
            return 0
        return int(max(bandwidths) * duration / 8)
    except Exception:
        return 0


#: Page titles that carry no video name — a player shell says one of
#: these while the real watch page (fetched earlier in the loop) has
#: the file's own title.
_GENERIC_TITLES = {"loading...", "loading", "play", "video", "player",
                   "lulustream video"}


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    code = _file_code(url)
    origin = _origin(url)
    tried = []
    best_title = ""
    for shape in _PAGE_SHAPES:
        page_url = shape.format(url=url, origin=origin, code=code)
        if page_url in tried:
            continue
        tried.append(page_url)
        try:
            final_url, html = await _fetch_page(page_url)
        except HostError:
            continue
        page_url = final_url or page_url
        title = _title_from_html(html)
        if title and title.lower() not in _GENERIC_TITLES and not best_title:
            best_title = title
        found = _find_in_page(html)
        if not found:
            continue
        media_url, duration = found
        media_url = normalise_url(media_url, page_url)
        is_playlist = media_url.lower().split("?")[0].endswith(".m3u8")
        # Both the shell and the player page can say just "Loading…":
        # name the file after the panel and its code instead of
        # saving "Loading....mp4".
        final_title = best_title or (
            "" if (title or "").lower() in _GENERIC_TITLES else title)
        if not final_title:
            panel = (urlparse(page_url).netloc or "lulustream").split(".")[0]
            final_title = f"{panel} {code}"
        return {
            "title": final_title,
            "media_url": media_url,
            "approx_size": await _approx_size(media_url, duration)
            if is_playlist else 0,
            "is_playlist": is_playlist,
            "referer": page_url,
            "duration": duration,
        }
    raise HostError(
        "couldn't find the video in the lulustream page — the site "
        "may have changed its player")


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    """Resolve the page and download the video; returns file paths."""
    info = await resolve(url)
    title = safe_filename(info["title"], "lulustream video")
    media_url = info["media_url"]
    referer = info.get("referer") or url
    if info["is_playlist"]:
        from core.extractors.ytdlp_engine import download as ytdlp_download
        paths = await ytdlp_download(media_url, job_dir,
                                     progress_cb=progress_cb,
                                     headers={"Referer": referer})
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
    ext = os.path.splitext(urlparse(media_url).path)[1].lower() or ".mp4"
    return [await download_file(media_url, job_dir, progress_cb=progress_cb,
                                filename=title + ext,
                                headers={"Referer": referer})]
