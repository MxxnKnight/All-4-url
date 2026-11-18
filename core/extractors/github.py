"""Custom extractor: github.com release links.

Why this exists: yt-dlp has no GitHub release extractor, and a
release page is not a file — the files are assets listed by GitHub's
public API (no auth; 60 requests/hour per IP unauthenticated, which
is plenty for one person's bot).

How it works:
  1. A /releases/download/<tag>/<file> link names one asset: the API
     finds that asset by name and returns its download URL.
  2. A /releases/tag/<tag> or /releases/latest page names a release:
     the API lists its assets and the LARGEST is picked (checksum and
     signature sidecars skipped — usually the main binary or archive;
     the asset name goes in the title so the owner can see what was
     picked, and a direct asset link always picks exactly).
  3. Anything else on github.com (repo pages, code, raw files) is not
     matched, so the normal chain handles it.

Verified live 2026-11-18 against yt-dlp's latest release via the
public API (tag 2026.10.26, assets listed with sizes and download
URLs).
"""

import json
from urllib.parse import unquote, urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import FallThrough, HostError

_API = "https://api.github.com"


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("github.com", "www.github.com"):
        return False
    return "/releases/" in urlparse(url).path


def _parse_release_url(url: str):
    """(owner, repo, kind, tag, filename) or None.

    kind is "download" (/releases/download/<tag>/<file>),
    "tag" (/releases/tag/<tag>) or "latest" (/releases/latest).
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 4 or parts[2] != "releases":
        return None
    owner, repo = parts[0], parts[1]
    if parts[3] == "download" and len(parts) >= 6:
        return owner, repo, "download", unquote(parts[4]), unquote("/".join(parts[5:]))
    if parts[3] == "tag" and len(parts) >= 5:
        return owner, repo, "tag", unquote("/".join(parts[4:])), None
    if parts[3] == "latest":
        return owner, repo, "latest", None, None
    return None


async def _get_json(url: str) -> tuple[int, dict | None]:
    """GET a GitHub API URL; returns (status, parsed body or None)."""
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": BROWSER_UA,
               "Accept": "application/vnd.github+json"}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                try:
                    return resp.status, await resp.json(content_type=None)
                except (ValueError, json.JSONDecodeError):
                    return resp.status, None
    except Exception as exc:
        raise HostError(f"couldn't reach GitHub ({exc})") from exc


def _is_checksum(name: str) -> bool:
    """True for checksum/signature sidecar files — never the prize."""
    low = (name or "").lower()
    return (low.endswith((".sig", ".asc", ".sha256", ".sha512", ".md5"))
            or "sums" in low and ("sha" in low or "md5" in low)
            or low in ("checksums.txt", "checksums"))


def _pick_asset(data: dict, want_name: str | None) -> dict | None:
    """The wanted asset by name, else the largest asset, else None.

    The largest pick skips checksum/signature sidecars (SHA2-256SUMS,
    .sig files) — nobody sends a release link for those.
    """
    assets = (data or {}).get("assets") or []
    if want_name:
        for asset in assets:
            if asset.get("name") == want_name:
                return asset
        return None
    real = [a for a in assets if not _is_checksum(a.get("name") or "")]
    if not real:
        return None
    return max(real, key=lambda a: int(a.get("size") or 0))


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist}."""
    parsed = _parse_release_url(url)
    if not parsed:
        raise FallThrough(
            "that github.com link isn't a single release — open the "
            "release itself (or a file in it) and send that link")
    owner, repo, kind, tag, filename = parsed
    if kind == "latest":
        api_url = f"{_API}/repos/{owner}/{repo}/releases/latest"
    else:
        api_url = f"{_API}/repos/{owner}/{repo}/releases/tags/{tag}"
    status, data = await _get_json(api_url)
    if status == 404:
        raise HostError(
            "GitHub says that release doesn't exist — check the tag, "
            "or the repo may be private")
    if status == 403:
        raise HostError(
            "GitHub's free API limit was hit for now — try again in "
            "a few minutes")
    if status != 200 or not isinstance(data, dict):
        raise HostError(f"GitHub's API answered HTTP {status}")
    asset = _pick_asset(data, filename)
    if not asset:
        if filename:
            raise HostError(
                f"that release has no file named {filename}")
        raise HostError("that GitHub release has no files attached")
    media_url = asset.get("browser_download_url") or ""
    if not media_url:
        raise HostError("GitHub gave no download link for that file")
    return {
        "title": asset.get("name") or filename or "github release",
        "media_url": media_url,
        "approx_size": int(asset.get("size") or 0),
        "is_playlist": False,
    }


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    # cancel_check is accepted for the extractor interface but
    # unused here: this module downloads directly, and a direct
    # download aborts when progress_cb raises JobCancelled.
    """Resolve the release and download the file; returns file paths."""
    info = await resolve(url)
    name = safe_filename(info["title"], "github release")
    return [await download_file(
        info["media_url"], job_dir, progress_cb=progress_cb,
        filename=name)]
