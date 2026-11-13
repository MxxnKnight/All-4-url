"""Custom extractors: file hosts and their "download gate" pages.

This is one browser-free downloader shared by the hosts whose page hides
the file behind a step or two: XFileSharing-style panels (StreamFile,
LuluStream, ImageTwist), YetiShare-style panels (TorUpload), Laravel
hosts (UpFiles), PHP panels (CloudFam) and shortlink domains that bounce
to one of them (d.torupload.com, upfilesgo.com, flash-files.com,
myupload.us).

How it works — a small, honest "gate walker" over plain HTTP:

  1. fetch the page the owner sent (cookies kept between steps),
  2. if the page already carries a media URL (player config, <video>,
     og:video, unpacked JS, Next.js payload) — use it,
  3. otherwise submit the page's download form once (XFileSharing's
     ``op=download1``, YetiShare's ``submitted=1``, Laravel's
     ``_token`` …) and look again,
  4. otherwise follow a meta-refresh / ``location.href`` bounce
     (that is what a shortlink page is),
  5. otherwise probe the page's "Download" links with a 1-byte Range
     request and keep the first one that really returns a file,
  6. give up with a plain explanation after a few steps.

No headless browser: a JS challenge that needs one (flash-files.com
answers Cloudflare's "Just a moment…") is reported as such instead of
pretending to work. Cookies from the walk are handed to the downloader
so hosts that lock the file to the session still serve it.

Testing note: this module is exercised against local replicas of each
template in the bot's test harness (XFileSharing gate → download2 form,
YetiShare ``submitted`` form, Laravel ``_token`` form, jwplayer page,
meta-refresh bounce, <img> page). It has not been run against a live
file on every host — no public file code was available — so a host that
changes its template can still fail; it fails with a message that says
which step it reached.
"""

import os
import re
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import (BROWSER_UA, download_file, safe_filename)
from core.extractors.player_scan import (HostError, download_links, find_media,
                                         gate_form, media_kind, next_page,
                                         page_title, request)

#: Host -> what the walker should know about it.
HOSTS = {
    # XFileSharing-style video panels.
    "streamfile.net": {"name": "StreamFile"},
    "luluvid.com": {"name": "LuluStream"},
    # XFileSharing-style *image* host: the "media" is one big <img>.
    "imagetwist.com": {"name": "ImageTwist", "images": True},
    # YetiShare panel + its shortlink sub-domain.
    "torupload.com": {"name": "TorUpload"},
    "d.torupload.com": {"name": "TorUpload"},
    # Laravel / custom PHP hosts.
    "upfiles.com": {"name": "UpFiles"},
    "upfilesgo.com": {"name": "UpFiles"},
    "cloudfam.io": {"name": "CloudFam"},
    # Cloudflare-challenged shortlink host (reported honestly if blocked).
    "flash-files.com": {"name": "FlashFiles"},
    # Next.js-hosted video/file panel.
    "myupload.us": {"name": "myUpload"},
}

#: Bare (scheme-less) or www-prefixed spellings of the same hosts.
for _host in list(HOSTS):
    HOSTS.setdefault("www." + _host, HOSTS[_host])

MAX_STEPS = 6


def is_site_url(url: str) -> bool:
    return (urlparse(url).netloc or "").lower() in HOSTS


def _profile(url: str) -> dict:
    return HOSTS.get((urlparse(url).netloc or "").lower(), {"name": "file host"})


# ------------------------------------------------------------------ probing

async def _is_media(url: str, referer: str) -> bool:
    """True when this URL really returns file bytes (1-byte probe)."""
    headers = {"User-Agent": BROWSER_UA, "Referer": referer,
               "Range": "bytes=0-0"}
    timeout = aiohttp.ClientTimeout(total=25, sock_read=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers,
                                   allow_redirects=True) as resp:
                if resp.status >= 400:
                    return False
                ctype = (resp.headers.get("Content-Type") or "").lower()
                disposition = resp.headers.get("Content-Disposition", "")
                if "text/html" in ctype and "attachment" not in disposition:
                    return False
                return any(hint in ctype for hint in
                           ("video/", "audio/", "image/", "application/octet-stream",
                            "application/mp4", "binary/octet-stream")) \
                    or "attachment" in disposition
    except Exception:
        return False


async def _size_of(url: str, referer: str) -> int:
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, allow_redirects=True,
                                    headers={"User-Agent": BROWSER_UA,
                                             "Referer": referer}) as resp:
                return int(resp.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def _jar_cookies(jar) -> dict:
    cookies = {}
    try:
        for cookie in jar:
            cookies[cookie.key] = cookie.value
    except Exception:
        return {}
    return cookies


def _result(media_url: str, page: str, page_url: str, jar, profile: dict) -> dict:
    is_playlist = media_url.lower().split("?")[0].endswith(".m3u8")
    return {
        "title": page_title(page) or f"{profile['name']} file",
        "media_url": media_url,
        "approx_size": 0,
        "is_playlist": is_playlist,
        "referer": page_url,
        "cookies": _jar_cookies(jar),
        "_walked": True,
    }


async def _submit(session, form: dict, page_url: str):
    """Submit a download-gate form; returns the next page's (url, body, status).

    The reply is handed back instead of being thrown away: a gate answers
    *with the next page* (or with the file itself), so re-fetching the
    original URL would just show the same form again.
    """
    fields = dict(form["fields"])
    action = form["action"] or page_url
    if form["method"] == "post":
        headers = {"Referer": page_url,
                   "Origin": f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        return await request(session, action, method="POST", data=fields,
                             headers=headers)
    # GET forms submit their fields as the query string — sending them
    # as `data` puts them in the request body, which servers ignore.
    return await request(session, action, method="GET", params=fields,
                         headers={"Referer": page_url})


async def _walk(url: str, *, images: bool = False) -> dict:
    """Run the gate walk; returns the resolved-media dict."""
    profile = _profile(url)
    timeout = aiohttp.ClientTimeout(total=60, sock_read=45)
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar) as session:
        page_url = url
        pending = None  # reply from a gate we just submitted
        for step in range(MAX_STEPS):
            if pending is not None:
                final, page, status = pending
                pending = None
            else:
                final, page, status = await request(
                    session, page_url,
                    headers={"Referer": page_url if step == 0 else url})
            page_url = final or page_url
            if status == 404:
                raise HostError(f"{profile['name']}: file not found — the "
                                "link is dead or the file was removed")
            if status >= 400:
                raise HostError(f"{profile['name']} answered HTTP {status}")
            if "file not found" in page.lower() or "page not found" in page.lower() \
                    or "could not be found" in page.lower():
                raise HostError(f"{profile['name']}: file not found — the "
                                "link is dead or the file was removed")

            # A gate can redirect straight at the bytes (Laravel hosts do).
            if media_kind(page_url) in ("file", "hls") and page_url != url:
                if await _is_media(page_url, url):
                    result = _result(page_url, page, page_url, jar, profile)
                    return result

            media = find_media(page, page_url, images=images or profile.get("images", False))
            if media:
                result = _result(media, page, page_url, jar, profile)
                result["approx_size"] = await _size_of(media, page_url)
                return result

            # A captcha is a hard wall for a browser-free walker —
            # Turnstile / reCAPTCHA / hCaptcha all need JavaScript in
            # a real browser, which this host can never run. Fail
            # honestly here instead of submitting forms at it and
            # looping until the step limit (proven on a real UpFiles
            # page, 2026-11-13: the free-download form leads to a
            # Cloudflare Turnstile).
            low = page.lower()
            if ("cf-turnstile" in low or "g-recaptcha" in low
                    or "h-captcha" in low):
                raise HostError(
                    f"{profile['name']}: this link needs a captcha "
                    f"check before the download — the bot can't do "
                    f"those, so download it in your browser and send "
                    f"the file to the bot")

            form = gate_form(page, page_url)
            if form:
                final, body, status = await _submit(session, form, page_url)
                if status >= 400:
                    raise HostError(f"the site's download step answered "
                                    f"HTTP {status}")
                pending = (final or page_url, body, status)
                page_url = pending[0]
                continue

            nxt = next_page(page, page_url, page_url)
            if nxt:
                page_url = nxt
                continue

            for link in download_links(page, page_url):
                if await _is_media(link, page_url):
                    result = _result(link, page, page_url, jar, profile)
                    return result

            raise HostError(
                f"{profile['name']}: couldn't find a download step on the page "
                f"(stopped after {step + 1} request(s)) — the site may have "
                "changed its template, or it needs JavaScript this host "
                "can't run")
    raise HostError(f"{profile['name']}: the download page kept redirecting "
                    f"without ever offering the file")


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    info = await _walk(url, images=_profile(url).get("images", False))
    return {key: info[key] for key in
            ("title", "media_url", "approx_size", "is_playlist")}


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    profile = _profile(url)
    info = await _walk(url, images=profile.get("images", False))
    title = safe_filename(info["title"], f"{profile['name']} file")
    media_url = info["media_url"]
    if info["is_playlist"]:
        from core.extractors.ytdlp_engine import download as ytdlp_download
        paths = await ytdlp_download(media_url, job_dir,
                                     progress_cb=progress_cb,
                                     headers={"Referer": info["referer"]})
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
    ext = os.path.splitext(urlparse(media_url).path)[1].lower()
    if ext not in (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".ts", ".mp3",
                   ".m4a", ".zip", ".rar", ".7z", ".pdf", ".jpg", ".jpeg",
                   ".png", ".gif", ".webp", ".apk", ".epub", ".srt"):
        ext = ".jpg" if profile.get("images") else ".mp4"
    return [await download_file(
        media_url, job_dir, progress_cb=progress_cb,
        filename=title + ext,
        headers={"Referer": info["referer"]},
        cookies=info.get("cookies") or None)]
