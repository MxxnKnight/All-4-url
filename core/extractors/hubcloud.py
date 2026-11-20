"""Custom extractor: hubcloud / hubdrive / gdflix (movie-link hosts).

Why this exists: these are the hosts behind most Indian
movie-download pages (HDHub4u, CineVood, Vegamovies and the like),
and no yt-dlp extractor understands them. Each family hides the
file behind its own small gate:

  * hubcloud — the page's ``#download`` button leads to a server
    list (``btn-lg`` links: an R2 presigned URL, pixeldrain, and
    player pages). The first server that actually serves bytes
    wins; player pages are filtered out by content-type.
  * hubdrive — POST ``/ajax.php?ajax=direct-download`` with the
    file id (``X-Requested-With: XMLHttpRequest``) returns JSON
    with a direct ``gd`` URL, the size, and the filename. If the
    API has no answer, the page's hubcloud mirror link is tried.
  * gdflix — the Instant DL button POSTs to ``/mfile/<id>`` with
    the page's own ``key`` (plus an empty Turnstile token) and
    the server answers JSON ``{url}`` — a direct download link.
    The file page may also carry a Telegram bot deep link
    ("Telegram Generate" button), returned as ``telegram_url``
    so the owner can take the link instead of the download
    (v1.50.0).

Two things this module deliberately does differently from the
usual mirror-bot approach:

  * Candidates are probed with a 1-byte Range GET, never HEAD.
    The R2 presigned URLs hubcloud serves are signed for GET
    only — HEAD answers 403 (verified 2026-11-16), so a HEAD
    probe would reject the one working server. A Range GET
    proves the bytes exist and reads the total size from
    ``Content-Range`` at the same time.
  * Domains churn (hubcloud.ist -> hubcloud.mov, hubdrive.pics ->
    hubdrive.blog, gdflix.dev -> new10.gdflix.io, all observed
    2026-11-16), so matching is by family name in the host, not
    an exact domain list, and every fetch follows redirects.

Packs (hubdrive ``/packs/``, gdflix ``/pack/``) list several
files; each item is resolved through the same single-file path
and all of them download.

Verified live 2026-11-16: hubcloud single file end-to-end (page
-> server list -> R2 presigned URL -> Range GET 206) and hubdrive
single file (ajax -> r2.dev URL -> Range GET 206). Gdflix could
NOT be verified then: its homepage loaded, but every file page
tried (5 links) answered HTTP 500 with an empty body.
Verified live 2026-11-20 (moon's pack link): the gdflix pack
page lists its 8 files, and one file page's POST /mfile/<id>
returned JSON {url} -> video-downloads.googleusercontent.com,
Range probe 200 with the full size. Full pack resolve verified
the same day against the real link: 8 files, 19.38 GB, every
file with a media URL and a Telegram deep link, in ~3 s.
The media URLs are short-lived (one expired within ~25 min,
answering 400) — downloads always re-resolve fresh.
"""

import html as html_lib
import os
import re
from urllib.parse import parse_qs, urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import (HostError, normalise_url, request
                                         as ps_request)

#: Most files a single pack share may download.
_MAX_FILES = 100
#: Servers this module cannot use (buzzheavier needs its own
#: resolver; vdplay / player pages are not downloads).
_SKIP_MARKERS = ("bzzhr.co", "vdplay")
#: gdflix's instant link is followed without redirects so the
#: Location header can be read.
_NO_REDIRECT_TIMEOUT = aiohttp.ClientTimeout(total=25)


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return ("hubcloud" in host or "hubdrive" in host
            or "gdflix" in host)


def _family(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "gdflix" in host:
        return "gdflix"
    if "hubdrive" in host:
        return "hubdrive"
    return "hubcloud"


# ------------------------------------------------------------------ html

_ANCHOR_RE = re.compile(r"<a\s[^>]*>", re.I)
_ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*"([^"]*)\"""")


def _anchors(html: str) -> list[dict]:
    """Every <a ...> tag as {href, class, id} (missing keys absent)."""
    out = []
    for tag in _ANCHOR_RE.findall(html or ""):
        attrs = dict(_ATTR_RE.findall(tag))
        if attrs.get("href"):
            out.append({"href": html_lib.unescape(attrs["href"]),
                        "class": attrs.get("class", ""),
                        "id": attrs.get("id", "")})
    return out


def _title(html: str, fallback: str) -> str:
    match = re.search(r"<title>([^<]+)</title>", html or "", re.I)
    if match:
        title = html_lib.unescape(match.group(1)).strip()
        title = re.sub(r"^(HubDrive|GDFlix)\s*\|\s*", "", title,
                       flags=re.I)
        if title:
            return title
    return fallback


_SIZE_RE = re.compile(r"([\d.,]+)\s*([KMGT]?B)", re.I)
_SIZE_MULT = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
              "TB": 1024 ** 4}


def _parse_size(text: str) -> int:
    """'693.49 MB' -> bytes. 0 when unparseable."""
    match = _SIZE_RE.search(text or "")
    if not match:
        return 0
    try:
        return int(float(match.group(1).replace(",", ""))
                   * _SIZE_MULT[match.group(2).upper()])
    except (ValueError, KeyError):
        return 0


# --------------------------------------------------------------- probing

async def _alive(session: aiohttp.ClientSession, url: str,
                 _depth: int = 0) -> tuple[str, int] | None:
    """(final_url, total_bytes) if `url` serves a file, else None.

    A 1-byte Range GET: it works on GET-signed presigned URLs
    where HEAD is rejected, and Content-Range carries the total.
    Only the headers are read — the body is NEVER consumed. A
    server that ignores Range and answers 200 with the whole
    file (video-downloads.googleusercontent.com does exactly
    this) would otherwise make the probe download gigabytes
    before the timeout kills it (v1.50.0: a gdflix pack probe
    stalled 20 s per file and blew the 45 s resolve budget).
    """
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.get(
                url, headers={"User-Agent": BROWSER_UA,
                              "Range": "bytes=0-0"},
                allow_redirects=True, timeout=timeout) as resp:
            final = str(resp.url)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if resp.status not in (200, 206) or "text/html" in ctype:
                return None
            total = 0
            if resp.status == 206:
                # "bytes 0-0/1940159590" — the part after "/" is the
                # whole file's size.
                tail = resp.headers.get("Content-Range", "").rsplit(
                    "/", 1)[-1]
                total = int(tail) if tail.isdigit() else 0
            else:
                length = resp.headers.get("Content-Length", "")
                total = int(length) if length.isdigit() else 0
    except Exception:
        return None
    # Some servers answer with a ?link=<real-url> wrapper.
    if _depth < 1:
        inner = parse_qs(urlparse(final).query).get("link")
        if inner:
            return await _alive(session, inner[0], _depth + 1)
    return final, total


async def _first_alive(session: aiohttp.ClientSession,
                       links: list[str]) -> tuple[str, int]:
    """First candidate that serves bytes; else the first candidate
    as a fallback (the download GET is the final judge)."""
    fallback = ""
    for link in links:
        if any(marker in link for marker in _SKIP_MARKERS):
            continue
        fallback = fallback or link
        found = await _alive(session, link)
        if found:
            return found
    if fallback:
        return fallback, 0
    raise HostError("none of the download servers listed for this "
                    "file could be reached")


async def _probe_size(session: aiohttp.ClientSession, url: str) -> int:
    found = await _alive(session, url)
    return found[1] if found else 0


# -------------------------------------------------------------- hubcloud

async def _hubcloud_links(session: aiohttp.ClientSession,
                          url: str) -> tuple[str, list[str]]:
    """(title, server links) from a hubcloud page."""
    _, html, status = await ps_request(session, url)
    if status >= 400:
        raise HostError(f"the hubcloud page returned HTTP {status} — "
                        f"the link may be dead or the file removed")
    title = _title(html, "hubcloud file")
    anchors = _anchors(html)
    target = next((a for a in anchors if a.get("id") == "download"), None)
    if target:
        _, html, status = await ps_request(
            session, normalise_url(target["href"], url))
        if status >= 400:
            raise HostError("hubcloud's download page returned "
                            f"HTTP {status}")
        anchors = _anchors(html)
    links = [normalise_url(a["href"], url) for a in anchors
             if "btn-lg" in a.get("class", "")
             and a["href"].startswith("http")]
    if not links:
        raise HostError(
            "hubcloud listed no download servers for this file — "
            "it may have expired or been removed")
    return title, links


async def _resolve_hubcloud(session: aiohttp.ClientSession,
                            url: str) -> dict:
    title, links = await _hubcloud_links(session, url)
    media_url, size = await _first_alive(session, links)
    return {"title": title, "media_url": media_url,
            "approx_size": size, "is_playlist": False}


# -------------------------------------------------------------- hubdrive

_PACK_TITLE_RE = re.compile(
    r"""class="[^"]*pack-clean-title[^"]*"[^>]*>([^<]+)""")
_PACK_SIZE_RE = re.compile(
    r"""class="[^"]*pack-clean-size[^"]*"[^>]*>([^<]+)""")
_PACK_LINK_RE = re.compile(
    r"""<a[^>]*class="[^"]*pack-clean-link[^"]*"[^>]*href="([^"]+)\"""")


async def _resolve_hubdrive(session: aiohttp.ClientSession,
                            url: str) -> dict:
    parsed = urlparse(url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    file_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    _, html, status = await ps_request(session, url)
    if status >= 400:
        raise HostError(f"the hubdrive page returned HTTP {status} — "
                        f"the link may be dead or the file removed")
    title = _title(html, "hubdrive file")
    if "/packs/" in url:
        # Pack: titles, sizes and links appear once per row, in
        # document order — zip them together.
        names = [html_lib.unescape(n).strip()
                 for n in _PACK_TITLE_RE.findall(html)]
        sizes = [_parse_size(s) for s in _PACK_SIZE_RE.findall(html)]
        hrefs = [html_lib.unescape(h) for h in _PACK_LINK_RE.findall(html)]
        files = []
        for name, href in zip(names, hrefs):
            if "pack" in href:
                continue
            item = await _resolve_hubdrive(
                session, normalise_url(href, origin))
            files.append({"name": name or item["title"],
                          "url": item["media_url"],
                          "size": item["approx_size"]})
        if not files:
            raise HostError("this hubdrive pack lists no files")
        total = sum(f["size"] for f in files) or sum(sizes)
        return {"title": title, "media_url": files[0]["url"],
                "approx_size": total, "is_playlist": False,
                "files": files}
    # The page's own direct-download API.
    try:
        _, body, ajax_status = await ps_request(
            session, f"{origin}/ajax.php?ajax=direct-download",
            method="POST", data={"id": file_id},
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Referer": url})
    except HostError:
        ajax_status, body = 0, ""
    if ajax_status == 200:
        try:
            import json

            data = json.loads(body)
            gd = (data.get("data") or {}).get("gd")
            if gd:
                name = (data.get("data") or {}).get("n") or title
                size = int((data.get("data") or {}).get("s") or 0)
                return {"title": name, "media_url": gd,
                        "approx_size": size, "is_playlist": False}
        except (ValueError, TypeError, AttributeError):
            pass
    # Fallback: the page's hubcloud mirror link.
    for anchor in _anchors(html):
        if "hubcloud" in anchor["href"]:
            return await _resolve_hubcloud(
                session, normalise_url(anchor["href"], url))
    raise HostError(
        "hubdrive wouldn't give a direct link for this file — "
        "it may have expired or been removed")


# ---------------------------------------------------------------- gdflix

_PACK_FILE_RE = re.compile(
    r"""<a[^>]*href="(/file/[^"]+)"[^>]*>(.*?)</a>""", re.S)

#: The download key the Instant DL script posts (one per page —
#: the pack page and each file page carry a different value, so
#: it is read from the page being resolved, never hardcoded).
_KEY_RE = re.compile(r"""formData\.append\("key",\s*"([0-9a-f]+)"\)""")


def _gdflix_telegram_link(html: str) -> str | None:
    """The file page's Telegram bot deep link ("Telegram Generate"
    button), if it has one. The channel links (t.me/gdflix_com,
    the release-group channel) are NOT file links — only a bot
    deep link (a ``?start=`` payload) counts."""
    for anchor in _anchors(html):
        if "_bot?start=" in anchor["href"]:
            return anchor["href"]
    return None


async def _gdflix_instant_post(session: aiohttp.ClientSession,
                               url: str, html: str) -> str | None:
    """Ask the server for the instant-download URL (the Instant DL
    button's POST, v1.50.0). Returns the URL, or None when the
    page has no key or the server gives no URL — the caller then
    falls back to the old plain instant anchor.
    ``action_token`` is the Turnstile token, empty until the
    challenge runs — the server accepts the POST without one
    (verified 2026-11-20 on new10.gdflix.io)."""
    import json

    match = _KEY_RE.search(html or "")
    if not match:
        return None
    parsed = urlparse(url)
    mfile = normalise_url(parsed.path.replace("file", "mfile", 1), url)
    try:
        _, body, status = await ps_request(
            session, mfile, method="POST",
            data={"action": "instant", "key": match.group(1),
                  "action_token": ""},
            headers={"Referer": url, "x-token": parsed.netloc})
    except Exception:
        return None
    if status >= 400:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    media = data.get("url") or data.get("visit_url")
    if isinstance(media, str) and media.strip():
        return media.strip()
    return None


async def _resolve_gdflix(session: aiohttp.ClientSession,
                          url: str, probe_size: bool = True) -> dict:
    # The page may redirect (gdflix.vip -> new10.gdflix.io): every
    # URL built from here on uses the FINAL host, not the input
    # one. A POST to the old host gets a 302, and following it
    # turns the POST into a GET — the server then answers HTML
    # instead of the JSON download link (v1.50.0).
    final, html, status = await ps_request(session, url)
    if status >= 400:
        raise HostError(f"the gdflix page returned HTTP {status} — "
                        f"the link may be dead or the file removed")
    parsed = urlparse(final)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    title = _title(html, "gdflix file")
    if "/pack/" in url:
        files = []
        for href, inner in _PACK_FILE_RE.findall(html):
            text = re.sub(r"<[^>]+>", " ", inner)
            text = html_lib.unescape(re.sub(r"\s+", " ", text)).strip()
            name, _, size_text = text.rpartition("[")
            # No per-file size probe here: the pack page lists each
            # file's size in the link text ("name.mkv [2.37GB]"),
            # and probing every Google URL would burn the resolve
            # budget (each probe can stall ~20 s when the host is
            # slow — v1.50.0).
            item = await _resolve_gdflix(
                session, normalise_url(href, origin), probe_size=False)
            files.append({"name": name.strip() or item["title"],
                          "url": item["media_url"],
                          "size": item["approx_size"]
                          or _parse_size(size_text.strip("] ")),
                          "telegram_url": item.get("telegram_url")})
        if not files:
            raise HostError("this gdflix pack lists no files")
        total = sum(f["size"] for f in files)
        return {"title": title, "media_url": files[0]["url"],
                "approx_size": total, "is_playlist": False,
                "files": files}
    media_url = await _gdflix_instant_post(session, final, html)
    if not media_url:
        # Fallback: the old plain instant anchor, in case some page
        # variant still has one (the pre-2026-11-20 layout).
        instant = next((a for a in _anchors(html)
                        if "instant" in a["href"]), None)
        if not instant:
            raise HostError(
                "gdflix shows no instant-download link for this file — "
                "it may have expired or been removed")
        try:
            async with session.get(
                    normalise_url(instant["href"], final),
                    headers={"User-Agent": BROWSER_UA, "Referer": final},
                    allow_redirects=False,
                    timeout=_NO_REDIRECT_TIMEOUT) as resp:
                location = (resp.headers.get("Location") or "").strip()
                await resp.read()
        except Exception as exc:
            raise HostError(f"couldn't follow gdflix's instant link "
                            f"({exc})") from exc
        if not location:
            raise HostError("gdflix's instant link led nowhere — the "
                            "file may have expired or been removed")
        unwrapped = parse_qs(urlparse(location).query).get("url")
        media_url = unwrapped[0] if unwrapped else location
    return {"title": title, "media_url": media_url,
            "approx_size": await _probe_size(session, media_url)
            if probe_size else 0,
            "is_playlist": False,
            "telegram_url": _gdflix_telegram_link(html)}


# ------------------------------------------------------------------ entry

async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}.

    Pack links add ``files`` (one row per file); download() uses
    it, single-file callers just use ``media_url``.
    """
    family = _family(url)
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if family == "gdflix":
            info = await _resolve_gdflix(session, url)
        elif family == "hubdrive":
            info = await _resolve_hubdrive(session, url)
        else:
            info = await _resolve_hubcloud(session, url)
    files = info.get("files")
    if files and len(files) > _MAX_FILES:
        raise HostError(
            f"this pack holds {len(files)} files — more than the "
            f"{_MAX_FILES}-file cap, so the bot won't download it "
            f"whole")
    return info


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    """Resolve the page and download the file(s); returns file paths."""
    info = await resolve(url)
    files = info.get("files")
    if not files:
        name = safe_filename(info["title"], "hubcloud file")
        ext = urlparse(info["media_url"]).path.rsplit(".", 1)[-1].lower()
        if ext and len(ext) <= 5 and not name.lower().endswith("." + ext):
            name += "." + ext
        return [await download_file(info["media_url"], job_dir,
                                    progress_cb=progress_cb, filename=name)]
    total = info["approx_size"] or sum(f["size"] for f in files)
    done_base = [0]
    paths: list[str] = []
    for entry in files:
        async def _progress(done: int, _total: int, _base=done_base[0]):
            if progress_cb:
                await progress_cb(_base + done, total)

        path = await download_file(
            entry["url"], job_dir, progress_cb=_progress,
            filename=safe_filename(entry["name"], "hubcloud file"))
        done_base[0] += entry["size"] or os.path.getsize(path)
        paths.append(path)
    return paths
