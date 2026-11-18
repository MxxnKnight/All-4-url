"""Custom extractor: gofile.io share links.

Why this exists: yt-dlp has no gofile extractor, and the share page
is a client-rendered app — its HTML has no file URL at all. The
site's own JSON API does the whole job with no account:

  1. POST https://api.gofile.io/accounts  -> a throwaway guest
     token (no signup, no email; one per resolve).
  2. GET https://api.gofile.io/contents/<code>?cache=true with
     ``Authorization: Bearer <token>`` and an ``X-Website-Token``
     header: the SHA-256 of
     ``"<UA>::en-US::<token>::<4h-slot>::<salt>"``. The salt lives
     in the site's obfuscated JS (``gofile.io/js/wt.obf.js``) and is
     scraped fresh each time, with a known-good fallback when the
     scrape fails.
  3. The reply lists the files with direct ``store-*.gofile.io``
     links. Downloading one needs the guest token back as an
     ``accountToken`` cookie, so resolve() carries it in the
     returned ``cookies`` dict and download() sends it.

Folders download as a whole: every public file inside (subfolders
included) is fetched, up to 100 files. Private folders, deleted
links, and password-protected shares fail with an honest error —
the bot has no password-entry flow.

Verified 2026-11-16: uploaded the bot's own 162,593-byte
sample.mp4 through the anonymous API and resolved the share link
back through this module's exact flow; the direct link Range-probed
206 live. Byte-identical download is covered by replay tests
(sandbox aiohttp cannot reach live sites, so the full download
through this module still needs a live-bot retest).
"""

import hashlib
import os
import re
import time
from urllib.parse import urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import FallThrough, HostError

_API = "https://api.gofile.io"
_WT_JS = "https://gofile.io/js/wt.obf.js"
#: Fallback website-token salt, taken from gofile's own JS. Used
#: only when the live scrape of wt.obf.js fails; verified working
#: 2026-11-16.
_FALLBACK_SALT = "12af056dacea0b"
#: Website-token slots are 4 hours long.
_WT_SLOT = 14400
#: Most files a single folder share may download.
_MAX_FILES = 100


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in ("gofile.io", "www.gofile.io")


def _code(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if not parts:
        raise HostError("that gofile link has no share code in it")
    code = parts[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        raise HostError("that gofile link has no usable share code in it")
    return code


async def _create_account(session: aiohttp.ClientSession) -> str:
    """Mint a throwaway guest token. Never raises HostError — an
    API hiccup here is a FallThrough, not a dead link."""
    try:
        async with session.post(
                f"{_API}/accounts",
                headers={"User-Agent": BROWSER_UA, "Accept": "*/*"}) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        raise FallThrough(f"gofile's API didn't answer ({exc})") from exc
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise FallThrough("gofile's API didn't hand out a guest token")
    token = (data.get("data") or {}).get("token")
    if not token:
        raise FallThrough("gofile's API didn't hand out a guest token")
    return token


async def _salt(session: aiohttp.ClientSession) -> str:
    """The website-token salt from gofile's obfuscated JS.

    Never raises: every failure falls back to the known-good
    salt, which still produces valid tokens (verified 2026-11-16).
    """
    try:
        async with session.get(
                _WT_JS, headers={"User-Agent": BROWSER_UA}) as resp:
            if resp.status >= 400:
                return _FALLBACK_SALT
            js = await resp.text(errors="ignore")
    except Exception:
        return _FALLBACK_SALT
    js = re.sub(r"\\x([0-9a-fA-F]{2})",
                lambda m: chr(int(m.group(1), 16)), js)
    windows = []
    idx = js.find("generateWT")
    if idx >= 0:
        windows.append(js[idx:])
    windows.append(js)
    for window in windows:
        for quote in ("'", '"'):
            match = re.search(quote + r"([0-9a-f]{14})" + quote, window)
            if match:
                return match.group(1)
    return _FALLBACK_SALT


async def _contents(session: aiohttp.ClientSession, token: str,
                    code: str, salt: str) -> dict:
    slot = int(time.time()) // _WT_SLOT
    raw = f"{BROWSER_UA}::en-US::{token}::{slot}::{salt}"
    wt = hashlib.sha256(raw.encode()).hexdigest()
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Authorization": "Bearer " + token,
        "X-Website-Token": wt,
        "X-BL": "en-US",
    }
    try:
        async with session.get(
                f"{_API}/contents/{code}?cache=true",
                headers=headers) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        raise FallThrough(f"gofile's API didn't answer ({exc})") from exc
    if not isinstance(data, dict):
        raise FallThrough("gofile's API returned an unexpected reply")
    status = data.get("status")
    if status == "ok":
        return data.get("data") or {}
    if status in ("error-passwordRequired", "error-passwordWrong"):
        raise HostError(
            "this gofile link needs a password, and the bot has "
            "no way to ask for one")
    if status == "error-notFound":
        raise HostError(
            "gofile says this file doesn't exist — the link may "
            "be dead or the file removed")
    if status == "error-notPublic":
        raise HostError("this gofile folder isn't public")
    raise FallThrough(f"gofile's API answered: {status or 'unknown error'}")


def _collect(node: dict, prefix: str, files: list) -> None:
    """Walk a contents node into flat {name, relpath, url, size} rows."""
    children = node.get("children")
    if not isinstance(children, dict):
        # A single file, not a folder.
        if node.get("link"):
            files.append({"name": node.get("name") or "gofile file",
                          "relpath": prefix,
                          "url": node["link"],
                          "size": int(node.get("size") or 0)})
        return
    for child in children.values():
        if not isinstance(child, dict):
            continue
        if child.get("type") == "folder":
            if not child.get("public", True):
                continue
            _collect(child,
                     "/".join(p for p in (prefix, child.get("name") or "")
                              if p),
                     files)
        elif child.get("link"):
            files.append({"name": child.get("name") or "gofile file",
                          "relpath": prefix,
                          "url": child["link"],
                          "size": int(child.get("size") or 0)})


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist, files, cookies}.

    ``files`` is the full list (one row per file, folders flattened);
    ``media_url`` is the first file's link for callers that only
    need one. ``cookies`` carries the guest token the direct links
    require — download() sends it back.
    """
    code = _code(url)
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = await _create_account(session)
        salt = await _salt(session)
        data = await _contents(session, token, code, salt)
    files: list = []
    _collect(data, "", files)
    if not files:
        raise HostError("this gofile folder is empty")
    if len(files) > _MAX_FILES:
        raise HostError(
            f"this gofile folder holds {len(files)} files — more "
            f"than the {_MAX_FILES}-file cap, so the bot won't "
            f"download it whole")
    total = sum(f["size"] for f in files)
    if data.get("type") == "folder" and data.get("name"):
        title = data["name"]
    else:
        title = files[0]["name"]
    return {"title": title, "media_url": files[0]["url"],
            "approx_size": total, "is_playlist": False,
            "files": files, "cookies": {"accountToken": token}}


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    """Resolve the share and download every file; returns file paths."""
    info = await resolve(url)
    files = info["files"]
    cookies = info["cookies"]
    total = info["approx_size"] or sum(f["size"] for f in files)
    done_base = [0]
    paths: list[str] = []
    for entry in files:
        subdir = os.path.join(job_dir, entry["relpath"]) \
            if entry["relpath"] else job_dir
        os.makedirs(subdir, exist_ok=True)

        async def _progress(done: int, _total: int, _base=done_base[0]):
            if progress_cb:
                await progress_cb(_base + done, total)

        path = await download_file(
            entry["url"], subdir, progress_cb=_progress,
            filename=safe_filename(entry["name"], "gofile file"),
            cookies=cookies)
        done_base[0] += entry["size"] or os.path.getsize(path)
        paths.append(path)
    return paths
