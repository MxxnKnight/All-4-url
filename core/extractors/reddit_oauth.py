"""Custom extractor: Reddit via its official OAuth API.

Why this exists: Reddit answers anonymous datacenter requests with
"blocked by network security", so the bot's gallery-dl/yt-dlp path
only works with a logged-in cookie jar — and even then it is slow
(Reddit throttles the server and the extractor makes many round
trips). The official API is a different door, built for bots: a
free app login (client ID + secret, created at reddit.com/prefs/
apps) gets a bearer token, and one API call returns the post's
media URLs. No cookies, no page scraping.

Needs REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in the environment
(Space Secrets). When they are not set, is_site_url returns False
and Reddit links keep flowing through gallery-dl exactly as today.

Deliberate exception to the registry's "never register a site
yt-dlp understands" rule: yt-dlp understands Reddit, but its path
is the blocked scrape. This extractor only claims a link when the
API answers with Reddit-hosted media; anything else raises
FallThrough and the chain continues to yt-dlp / gallery-dl, so a
Reddit link to a YouTube video still reaches yt-dlp, and a post
the API cannot see still gets its cookie-backed chance.

Media plan (mirrors what the post's API data carries):
- video: media.reddit_video.dash_url (DASH manifest, video+audio)
  downloaded by yt-dlp, which merges the tracks; hls_url next;
  fallback_url (video-only MP4) last. preview.reddit_video_preview
  covers gfycat/redgifs embeds the same way.
- gallery: gallery_data + media_metadata, each item's source URL.
- image: the post's own URL (i.redd.it original, or wherever the
  post points when post_hint says image).
- GIF: preview.images[0].variants.mp4.
Crossposts fall back to the parent post's media. Sizes are not in
the API data, so approx_size is 0 (unknown) — the split step after
download still caps oversize files, exactly like the gallery and
scrape flows.
"""

import html
import os
import re
import time
from urllib.parse import urlparse

import aiohttp

import config
from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import FallThrough

#: Hosts that serve Reddit post links (incl. the share domains).
REDDIT_HOSTS = (
    "reddit.com", "www.reddit.com",
    "old.reddit.com", "new.reddit.com",
    "sh.reddit.com",
    "redd.it", "www.redd.it",
)

#: Where the API token comes from, and where post data comes from.
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_URL = "https://oauth.reddit.com/api/info/?id=t3_"

#: Image extensions a post URL may carry (preview.redd.it included).
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

_COMMENT_RE = re.compile(r"/comments/([A-Za-z0-9]+)")
_GALLERY_RE = re.compile(r"/gallery/([A-Za-z0-9]+)")

#: Cached bearer token: {"value": str|None, "expires_at": epoch}.
#: The bot runs one event loop, so a plain dict is safe here.
_TOKEN = {"value": None, "expires_at": 0.0}


def _configured() -> bool:
    return bool(config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET)


def _user_agent() -> str:
    # Reddit asks API apps to identify themselves; the version rides
    # along so a bad actor can't be mistaken for this bot.
    return (f"python:all4url:{config.BOT_VERSION} "
            f"(personal-use Telegram uploader)")


def is_site_url(url: str) -> bool:
    if not _configured():
        return False
    host = (urlparse(url).netloc or "").lower()
    return host in REDDIT_HOSTS


def _post_id(url: str):
    """Return (post_id, is_share_link).

    /s/ share links only reveal their post id after following the
    redirect to the canonical comments URL, so they come back with
    is_share_link True and the caller resolves them first.
    """
    parsed = urlparse(url)
    path = parsed.path or ""
    match = _COMMENT_RE.search(path) or _GALLERY_RE.search(path)
    if match:
        return match.group(1), False
    parts = [p for p in path.strip("/").split("/") if p]
    host = (parsed.netloc or "").lower()
    if host in ("redd.it", "www.redd.it") and len(parts) == 1:
        return parts[0], False
    if len(parts) >= 2 and parts[-2] == "s":
        # /r/<sub>/s/<code> (or a bare /s/<code>) — the share code
        # only becomes a post id after following the redirect.
        return parts[-1], True
    raise FallThrough("that's not a single Reddit post link")


async def _resolve_share(url: str) -> str:
    """Follow a /s/ share link to its canonical comments URL."""
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": BROWSER_UA},
                                   allow_redirects=True) as resp:
                final = str(resp.url)
    except Exception as exc:
        raise FallThrough(
            f"couldn't open that Reddit share link ({exc})") from exc
    if not _COMMENT_RE.search(urlparse(final).path or ""):
        raise FallThrough("that Reddit share link didn't lead to a post")
    return final


async def _get_token(session: aiohttp.ClientSession) -> str:
    """App-only bearer token, cached until a minute before expiry."""
    now = time.time()
    if _TOKEN["value"] and now < _TOKEN["expires_at"] - 60:
        return _TOKEN["value"]
    auth = aiohttp.BasicAuth(config.REDDIT_CLIENT_ID,
                             config.REDDIT_CLIENT_SECRET)
    try:
        async with session.post(
                _TOKEN_URL, auth=auth,
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": _user_agent()}) as resp:
            status = resp.status
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
    except Exception as exc:
        raise FallThrough(f"couldn't reach Reddit's API ({exc})") from exc
    token = (data or {}).get("access_token") if isinstance(data, dict) else None
    if status != 200 or not token:
        raise FallThrough(
            "Reddit wouldn't hand over an API token — check the app "
            "id/secret in the Space Settings (REDDIT_CLIENT_ID / "
            "REDDIT_CLIENT_SECRET)")
    _TOKEN["value"] = token
    _TOKEN["expires_at"] = now + float(data.get("expires_in") or 3600)
    return token


async def _fetch_post(session: aiohttp.ClientSession,
                      post_id: str) -> dict:
    """One post's API data. A 401 refreshes the token once and
    retries; anything else the API refuses is a FallThrough — the
    chain's cookie-backed engines still get their turn."""
    token = await _get_token(session)
    headers = {"Authorization": f"Bearer {token}",
               "User-Agent": _user_agent()}
    url = _API_URL + post_id
    for attempt in (1, 2):
        try:
            async with session.get(url, headers=headers) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
        except Exception as exc:
            raise FallThrough(
                f"couldn't ask Reddit about that post ({exc})") from exc
        if status == 401 and attempt == 1:
            _TOKEN["value"] = None
            _TOKEN["expires_at"] = 0.0
            token = await _get_token(session)
            headers["Authorization"] = f"Bearer {token}"
            continue
        if status == 429:
            raise FallThrough(
                "Reddit says slow down (rate limit) — try again in a bit")
        if status != 200 or not isinstance(data, dict):
            raise FallThrough(
                f"Reddit's API answered HTTP {status} for that post")
        children = (data.get("data") or {}).get("children") or []
        if not children:
            raise FallThrough(
                "that Reddit post is gone, deleted, or private")
        return children[0].get("data") or {}
    raise FallThrough("Reddit's API wouldn't answer for that post")


def _is_image_url(dest: str) -> bool:
    return urlparse(dest or "").path.lower().endswith(_IMAGE_EXTS)


def _has_media(post: dict) -> bool:
    return bool(post.get("media") or post.get("secure_media")
                or post.get("is_gallery") or post.get("preview"))


def _extract_media(post: dict) -> dict | None:
    """Pick the downloadable media out of a post's API data.

    Returns a plan — {"kind": "dash"|"file", "media_url": ...,
    "title": ...} or {"kind": "images", "media_urls": [...],
    "title": ...} — or None when the post has no media hosted on
    Reddit itself (text post, link post). Pure function: no
    network, so the tests feed it fabricated post data.
    """
    if not _has_media(post):
        parents = post.get("crosspost_parent_list") or []
        if parents:
            post = parents[0]
    title = (post.get("title") or "Reddit post").strip() or "Reddit post"
    media = post.get("media") or post.get("secure_media") or {}
    video = (media.get("reddit_video") or {}) \
        if isinstance(media, dict) else {}
    if video.get("dash_url") or video.get("hls_url"):
        return {"kind": "dash",
                "media_url": html.unescape(video.get("dash_url")
                                           or video.get("hls_url")),
                "title": title}
    if video.get("fallback_url"):
        return {"kind": "file",
                "media_url": html.unescape(video["fallback_url"]),
                "title": title}
    preview = post.get("preview") or {}
    rvp = preview.get("reddit_video_preview") or {}
    if rvp.get("dash_url") or rvp.get("hls_url"):
        return {"kind": "dash",
                "media_url": html.unescape(rvp.get("dash_url")
                                           or rvp.get("hls_url")),
                "title": title}
    if rvp.get("fallback_url"):
        return {"kind": "file",
                "media_url": html.unescape(rvp["fallback_url"]),
                "title": title}
    if post.get("is_gallery"):
        meta = post.get("media_metadata") or {}
        items = (post.get("gallery_data") or {}).get("items") or []
        urls = []
        for item in items:
            src = ((meta.get(item.get("media_id") or "") or {}).get("s")
                   or {}).get("u")
            if src:
                urls.append(html.unescape(src))
        if urls:
            return {"kind": "images", "media_urls": urls, "title": title}
    dest = post.get("url_overridden_by_dest") or post.get("url") or ""
    if dest and (post.get("post_hint") == "image"
                 or _is_image_url(dest)):
        return {"kind": "images",
                "media_urls": [html.unescape(dest)], "title": title}
    images = preview.get("images") or []
    if images:
        mp4 = ((images[0].get("variants") or {}).get("mp4") or {})
        src = (mp4.get("source") or {}).get("url")
        if src:
            return {"kind": "file", "media_url": html.unescape(src),
                    "title": title}
    return None


async def resolve(url: str) -> dict:
    if not _configured():
        raise FallThrough("Reddit's API login isn't set up "
                          "(REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET)")
    post_id, is_share = _post_id(url)
    if is_share:
        url = await _resolve_share(url)
        post_id, _ = _post_id(url)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        post = await _fetch_post(session, post_id)
    plan = _extract_media(post)
    if plan is None:
        raise FallThrough("that Reddit post has no photo or video "
                          "hosted on Reddit itself")
    info = {
        "title": plan["title"],
        "media_url": plan.get("media_url")
        or (plan.get("media_urls") or [""])[0],
        # The API carries no byte sizes — the split step after the
        # download still caps oversize files, as in the gallery and
        # scrape flows.
        "approx_size": 0,
        "is_playlist": False,
    }
    info.update(plan)
    return info


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    info = await resolve(url)
    title = safe_filename(info["title"], "reddit post")
    kind = info.get("kind")
    if kind == "dash":
        # A bare manifest has no title, so yt-dlp names the file
        # after the manifest — rename it to the post title, as
        # vidara does for its playlists.
        from core.extractors.ytdlp_engine import download as ytdlp_download
        paths = await ytdlp_download(info["media_url"], job_dir,
                                     progress_cb=progress_cb)
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
    if kind == "file":
        media_url = info["media_url"]
        ext = os.path.splitext(urlparse(media_url).path)[1].lower()
        if ext not in (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".gif"):
            ext = ".mp4"
        return [await download_file(media_url, job_dir,
                                    progress_cb=progress_cb,
                                    filename=title + ext)]
    paths = []
    urls = info.get("media_urls") or []
    for i, img_url in enumerate(urls):
        ext = os.path.splitext(urlparse(img_url).path)[1].lower()
        if ext not in _IMAGE_EXTS:
            ext = ".jpg"
        name = title if len(urls) == 1 else f"{title}_{i + 1}"
        paths.append(await download_file(img_url, job_dir,
                                         progress_cb=progress_cb,
                                         filename=name + ext))
    return paths
