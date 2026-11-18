"""Custom extractor: Stripchat live cams (stripchat.com/<model>).

Why this exists: yt-dlp's Stripchat extractor refuses every model the
moment the page carries a ``viewCam.show`` object —

    if traverse_obj(data, ('viewCam', 'show', {dict})):
        raise ExtractorError('Model is in a private show')

That check is out of date. Stripchat leaves the *last* show object in
the page state after the show has ended (``show.endedAt`` is in the
past), for public free models too, so a plain free model is reported
as "paid/private" and never recorded. Measured 2026-11-18 on five live
public models: every page had ``viewCam.model.isLive = True``,
``viewCam.model.status = "public"`` and a ``show`` object whose
``endedAt`` was already in the past (modes ``private``,
``virtualPrivate``, ``groupShow``, ``p2p``). The correct gate is the
model's own live/status pair, not the presence of ``show``.

The second half is the playlist itself. DoppioCDN answers the *bare*
variant URL with a 291-byte advert playlist

    #EXT-X-MOUFLON-ADVERT / #EXT-X-PLAYLIST-TYPE:VOD / chunk_000.m4s

so anything that just follows the master playlist records an advert,
not the model. The real variant needs the tokens from the master's
``#EXT-X-MOUFLON:PSCH:v2:<pkey>`` lines appended as
``&psch=v2&pkey=<pkey>``. With the tokens the playlist is live
(MEDIA-SEQUENCE moves, no ENDLIST) and its segment URIs are hidden in
``#EXT-X-MOUFLON:URI:<url>`` lines that have to be decrypted (Mouflon
v2: reverse the token, base64-decode it, XOR with SHA-256 of a
"pdkey"): the plain ``media.mp4`` placeholder on the following line is
replaced by the decoded URL.

pdkeys rotate and are not published by Stripchat; the bot takes one
from ``STRIPCHAT_PDKEY`` (env / Space secret), from
``data/stripchat_keys.json`` (``{"<pkey>": "<pdkey>"}``), or from the
small built-in list, and proves a candidate on a real token before
using it. A live playlist has to be re-fetched and re-decrypted for
every segment, so ``open_stream()`` (used by /live) starts a tiny
loopback HLS proxy that does exactly that for ffmpeg; a one-shot
download of a live stream cannot work.

Verified 2026-11-18: page state, live/public status, master playlist,
token-minted live playlist (200, MEDIA-SEQUENCE 2489, 3 segments) on
several public models. NOT verified end to end: this sandbox's IP gets
403 from the CDN after a handful of playlist fetches, and no pdkey was
available, so the decrypted recording itself is untested here.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import socket
from urllib.parse import quote, unquote, urlparse

import aiohttp
from aiohttp import web

from core.extractors.direct import BROWSER_UA
from core.extractors.player_scan import HostError, fetch_page, page_title

#: Hosts that serve model pages (the www. form is what search hits).
HOSTS = ("stripchat.com", "www.stripchat.com")

#: DoppioCDN hosts a playlist or segment may legitimately live on. The
#: proxy refuses anything else (it is an open port on the bot's box).
_CDN_PREFIXES = ("edge-hls.doppiocdn.", "media-hls.doppiocdn.")

#: CDN hostnames tried when the page state names none.
_DEFAULT_HOSTS = ("doppiocdn.net", "doppiocdn.com", "doppiocdn.media")

#: The HTML Accept header matters: a bare "Accept: */*" gets a 406.
_ACCEPT_HTML = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                "*/*;q=0.8")

#: How many times a variant is re-fetched when the CDN hands back its
#: advert playlist (it does that when the psch/pkey pair is missing or
#: stale) before the extractor gives up on that host.
_AD_RETRIES = 2

#: Known Mouflon v2 pkey→pdkey pairs, published by the community that
#: reverse engineered the player. They rotate, so every one of these is
#: a *candidate*: only a pair that actually decrypts a token is used.
#: Measured 2026-11-18: the master playlist lists ~11 pkeys and the CDN
#: honours ANY of them — asking for the variant with a known pair returns
#: the same live stream encrypted under that pair's key, which is why a
#: known pair is tried before the one the master puts next to the
#: variant.
_KNOWN_PAIRS = (
    ("1Dzcc6OjP73LKbtI", "Y64UVwX5RrIWnOLp"),
    ("Ook7quaiNgiyuhai", "EQueeGh2kaewa3ch"),
    ("Zeechoej4aleeshi", "ubahjae7goPoodi6"),
    ("Zokee2OhPh9kugh4", "Quean4cai9boJa5a"),
)

#: The pdkeys of those pairs, tried against any playlist.
_KNOWN_PDKEYS = tuple(pdkey for _pkey, pdkey in _KNOWN_PAIRS)

#: 16 characters, alphanumeric — the shape of a Mouflon token/key.
_TOKEN_RE = re.compile(r"_(\d+)_([^_/]+)_(\d+)")
_KEY_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")

_STATE_RE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*")
_PSCH_RE = re.compile(r"^#EXT-X-MOUFLON:PSCH:(\w+):(\w+)\s*$")
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:.*?RESOLUTION=(\d+)x(\d+)")
_MOUFLON_URI = "#EXT-X-MOUFLON:URI:"
_MAP_URI_RE = re.compile(r'URI="([^"]+)"')


# --------------------------------------------------------------- helpers

def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host in HOSTS


def _model_slug(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    # /en/<model>, /<model>, /<model>/... — the model is the last
    # alphabetic segment; numeric/known app paths are not model pages.
    for part in reversed(parts):
        if re.fullmatch(r"[A-Za-z0-9_-]{3,40}", part) and \
                part.lower() not in ("en", "ru", "de", "es", "fr", "it",
                                     "pt", "pt-br", "api", "tags", "girls",
                                     "guys", "couples", "trans", "login",
                                     "signup", "broadcast", "settings"):
            return part
    raise HostError("that stripchat link has no model name in it")


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc or 'stripchat.com'}"


def _headers(referer: str, accept: str = _ACCEPT_HTML) -> dict:
    return {"User-Agent": BROWSER_UA, "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9", "Referer": referer,
            "Origin": "https://stripchat.com"}


def _preloaded_state(page: str) -> dict | None:
    """Cut window.__PRELOADED_STATE__ out of the page and parse it.

    Brace scanning beats a regex: the JSON is ~280 KB with nested
    strings that contain braces.
    """
    match = _STATE_RE.search(page or "")
    if not match:
        return None
    i, depth, in_string, escaped, start = match.end(), 0, False, False, match.end()
    while i < len(page):
        ch = page[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    try:
        return json.loads(page[start:i])
    except Exception:
        return None


def _model_info(state: dict) -> dict:
    view = state.get("viewCam") or {}
    model = view.get("model") or {}
    if not isinstance(model, dict):
        return {}
    return model


def _stream_hosts(state: dict) -> list[str]:
    config = (state.get("configV3") or {})
    common = config.get("initialCommon") or {}
    static = config.get("static") or {}
    features = static.get("featureSettings") or {}
    fallback = ((features.get("hlsFallback") or {}).get("fallbackDomains")
                or [])
    found = [common.get("hlsStreamHost"), common.get("streamHost")]
    found += list(fallback)
    hosts: list[str] = []
    for host in found:
        host = (host or "").strip().lower()
        if host and host not in hosts:
            hosts.append(host)
    for host in _DEFAULT_HOSTS:
        if host not in hosts:
            hosts.append(host)
    return hosts


def _variant_plan(master: str) -> list[dict]:
    """Playable variants, each with the psch/pkey pair that mints it.

    The master lists PSCH lines *before* the STREAM-INF they apply to;
    the last pair seen is the one for the next variant. ``pairs`` keeps
    every pair the master offered, best first: the ones we hold a
    decryption key for, then the rest in page order.
    """
    plan, psch, pkey = [], None, None
    pairs: list[tuple[str, str]] = []
    known = {pkey for pkey, _pdkey in _KNOWN_PAIRS}
    lines = (master or "").splitlines()
    for idx, line in enumerate(lines):
        line = line.strip()
        pair = _PSCH_RE.match(line)
        if pair:
            psch, pkey = pair.group(1), pair.group(2)
            if (psch, pkey) not in pairs:
                pairs.append((psch, pkey))
            continue
        stream = _STREAM_INF_RE.match(line)
        if not stream or idx + 1 >= len(lines):
            continue
        url = lines[idx + 1].strip()
        if not url.startswith("http"):
            continue
        plan.append({"url": url, "psch": psch, "pkey": pkey,
                     "width": int(stream.group(1)),
                     "height": int(stream.group(2))})
    pairs.sort(key=lambda p: p[1] not in known)
    for variant in plan:
        variant["pairs"] = pairs
    plan.sort(key=lambda v: v["height"], reverse=True)
    return plan


def _tokenized(url: str, psch: str | None, pkey: str | None) -> str:
    """The variant URL plus the tokens that make the CDN serve it live.

    Without them the CDN answers its advert playlist instead.
    """
    if not pkey:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}psch={psch or 'v2'}&pkey={pkey}"


def _with_tokens(variant: dict) -> str:
    return _tokenized(variant["url"], variant.get("psch"),
                      variant.get("pkey"))


def _is_advert(playlist: str) -> bool:
    return ("MOUFLON-ADVERT" in playlist
            or ("#EXT-X-PLAYLIST-TYPE:VOD" in playlist
                and "/cpa/" in playlist))


def _is_live(playlist: str) -> bool:
    return "#EXTM3U" in playlist and "#EXT-X-ENDLIST" not in playlist


# ------------------------------------------------------- Mouflon v2 keys

def _pad_b64(value: str) -> str:
    return value + "=" * ((4 - len(value) % 4) % 4)


def _xor_decrypt(token: str, key: str) -> str | None:
    try:
        data = base64.b64decode(_pad_b64(token))
    except Exception:
        return None
    if not data:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    plain = bytes(byte ^ digest[i % len(digest)]
                  for i, byte in enumerate(data))
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _decrypt_token(token: str, key: str) -> str | None:
    plain = _xor_decrypt(token[::-1], key)
    if not plain:
        return None
    # A wrong key yields control bytes or a string no URL ever uses.
    if any(ord(ch) < 0x20 or ord(ch) > 0x7e for ch in plain):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", plain):
        return None
    return plain


def _decrypt_uri(uri: str, key: str) -> str | None:
    match = _TOKEN_RE.search(uri)
    if not match:
        return None
    token = match.group(2)
    plain = _decrypt_token(token, key)
    if not plain:
        return None
    return uri.replace(f"_{token}_", f"_{plain}_")


def _candidate_keys() -> list[str]:
    keys: list[str] = []

    def add(value):
        value = (value or "").strip()
        if value and value not in keys and _KEY_RE.match(value):
            keys.append(value)

    env = os.environ.get("STRIPCHAT_PDKEY", "")
    if ":" in env:  # "pkey:pdkey" pasted from the community thread
        add(env.split(":", 1)[1])
    else:
        add(env)
    for path in ("data/stripchat_keys.json", "stripchat_keys.json"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if isinstance(data, dict):
            for value in data.values():
                add(str(value))
        elif isinstance(data, list):
            for value in data:
                add(str(value))
    for value in _KNOWN_PDKEYS:
        add(value)
    return keys


def pick_key(playlist: str) -> str | None:
    """First candidate key that really decrypts a token in `playlist`."""
    sample = None
    for line in (playlist or "").splitlines():
        if line.startswith(_MOUFLON_URI):
            sample = line[len(_MOUFLON_URI):].strip()
            break
    if not sample:
        return None
    for key in _candidate_keys():
        if _decrypt_uri(sample, key):
            return key
    return None


def decode_playlist(playlist: str, key: str) -> str:
    """Rewrite a Mouflon playlist into plain HLS (absolute URLs).

    Each ``#EXT-X-MOUFLON:URI:<url>`` line hides the segment URL and is
    followed by a ``media.mp4`` placeholder; the decoded URL replaces
    the placeholder. Unknown MOUFLON tags are dropped.
    """
    out: list[str] = []
    pending: str | None = None
    for line in (playlist or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(_MOUFLON_URI):
            uri = stripped[len(_MOUFLON_URI):].strip()
            pending = _decrypt_uri(uri, key) or pending
            continue
        if stripped.startswith("#EXT-X-MOUFLON"):
            continue
        if not stripped:
            out.append(line)
            continue
        if not stripped.startswith("#"):
            # A segment URI (usually the media.mp4 placeholder).
            out.append(pending or stripped)
            pending = None
            continue
        out.append(line)
    if pending:
        out.append(pending)
    return "\n".join(out)


# ------------------------------------------------------------ resolving

async def _page_state(page_url: str) -> dict:
    final, page = await fetch_page(page_url, headers={
        "Accept": _ACCEPT_HTML, "Accept-Language": "en-US,en;q=0.9"})
    state = _preloaded_state(page)
    if not state:
        if "isLive" in page and "viewCam" in page:
            raise HostError("stripchat's page layout changed — the live "
                            "state is no longer where I read it")
        raise HostError("stripchat returned a page I couldn't read")
    return state


async def _get_playlist(url: str, referer: str, timeout: int = 25) -> str:
    """GET a playlist/JSON body with the browser headers the CDN wants."""
    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url, headers=_headers(referer),
                                   allow_redirects=True) as resp:
                body = await resp.text(errors="replace")
                if resp.status >= 400:
                    raise HostError(f"the stream host answered HTTP "
                                    f"{resp.status}")
                return body
    except HostError:
        raise
    except Exception as exc:
        raise HostError(f"couldn't reach stripchat's stream host ({exc})") from exc


async def _master_urls(model_id: int, hosts: list[str]):
    return [f"https://edge-hls.{host}/hls/{model_id}/master/"
            f"{model_id}_auto.m3u8" for host in hosts]


#: Playlist fetches one resolve may make. Every extra attempt costs a
#: request against the CDN's rate limit, and the first known pair
#: normally answers straight away.
_MAX_FETCHES = 8


async def _probe_segment(playlist: str, pdkey: str, referer: str) -> bool:
    """True when a decoded segment really downloads.

    The last check between a guessed key and a recording that writes
    nothing: a wrong key produces a URL the CDN 404s.
    """
    plain = decode_playlist(playlist, pdkey)
    for line in plain.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("http"):
            continue
        headers = {"User-Agent": BROWSER_UA, "Referer": referer,
                   "Origin": "https://stripchat.com",
                   "Range": "bytes=0-4095"}
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(line, headers=headers) as resp:
                    if resp.status >= 400:
                        return False
                    body = await resp.content.read(4096)
                    return bool(body) and b"<html" not in body[:64].lower()
        except Exception:
            return False
    return False


async def _live_playlist(model_id: int, hosts: list[str],
                         referer: str) -> dict:
    """Find a variant playlist that is live, not the advert.

    Returns {variant, playlist, pdkey, height}. Every psch/pkey pair
    from the master is tried, the pairs whose key we hold first — the
    CDN mints the same live stream under whichever pair is asked for,
    so a known pair is what makes the playlist decryptable.
    """
    errors: list[str] = []
    encrypted_only = False
    fetches = 0
    for master_url in await _master_urls(model_id, hosts):
        try:
            master = await _get_playlist(master_url, referer)
        except HostError as exc:
            errors.append(str(exc))
            continue
        fetches += 1
        plan = _variant_plan(master)
        if not plan:
            errors.append("the stream host listed no playable quality")
            continue
        for variant in plan:
            for psch, pkey in variant.get("pairs") or [(variant.get("psch"),
                                                        variant.get("pkey"))]:
                if fetches >= _MAX_FETCHES:
                    break
                url = _tokenized(variant["url"], psch, pkey)
                playlist = ""
                for attempt in range(_AD_RETRIES + 1):
                    playlist = await _get_playlist(url, referer)
                    fetches += 1
                    if not _is_advert(playlist):
                        break
                    # The CDN hands out its advert when the tokens did
                    # not register; a re-fetch usually mints them.
                    await asyncio.sleep(1 + attempt)
                if not playlist or _is_advert(playlist) or not _is_live(playlist):
                    continue
                base = {"variant": url, "playlist": playlist,
                        "psch": psch, "pkey": pkey,
                        "width": variant["width"],
                        "height": variant["height"], "pdkey": None}
                if _MOUFLON_URI not in playlist:
                    return base
                key = pick_key(playlist)
                if not key:
                    encrypted_only = True
                    continue
                if not await _probe_segment(playlist, key, referer):
                    encrypted_only = True
                    continue
                return {**base, "pdkey": key}
            if fetches >= _MAX_FETCHES:
                break
        errors.append("the stream host only offered adverts")
    if encrypted_only:
        raise HostError(
            "stripchat encrypts its live playlist (Mouflon v2) and none of "
            "the decryption keys I know matches it — add the current "
            "pkey:pdkey to data/stripchat_keys.json or set STRIPCHAT_PDKEY")
    raise HostError(
        "stripchat's stream host wouldn't give me the live playlist"
        + (f" ({errors[-1]})" if errors else ""))


async def resolve(url: str) -> dict:
    """Return {title, media_url, is_playlist, referer, live, encrypted}.

    Raises HostError for an offline model, a paid/private show, or a
    stream the CDN will not hand over.
    """
    slug = _model_slug(url)
    referer = f"https://stripchat.com/{slug}"
    state = await _page_state(referer)
    model = _model_info(state)
    if not model:
        raise HostError("that stripchat model page had no stream state in it")
    if not model.get("isLive"):
        raise HostError(f"{slug} isn't live right now — nothing to record")
    status = str(model.get("status") or "").lower()
    if status and status != "public":
        # The API's own classification: anything but "public" means the
        # model is in a paid/private/group show, which isn't recordable.
        raise HostError(f"{slug} is in a {status} show — that one is paid "
                        f"and can't be recorded")
    model_id = model.get("id") or model.get("userId")
    if not model_id:
        raise HostError("that stripchat page had no model id in it")

    found = await _live_playlist(int(model_id), _stream_hosts(state), referer)
    info = {
        "title": f"{slug} — Stripchat live",
        "media_url": found["variant"],
        "approx_size": 0,
        "is_playlist": True,
        "referer": referer,
        "live": True,
        "encrypted": bool(found.get("pdkey")),
        "model_id": int(model_id),
        "height": found["height"],
    }
    if found.get("pdkey"):
        info["pdkey"] = found["pdkey"]
    return info


# --------------------------------------------------- loopback HLS proxy

#: One proxy for the whole process: ffmpeg reads plain HLS from it.
_PROXY: dict = {"runner": None, "port": 0, "streams": {}}


def _port_open() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _cdn_allowed(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return any(host.startswith(prefix) for prefix in _CDN_PREFIXES)


def _proxy_url(token: str, media_url: str) -> str:
    return (f"http://127.0.0.1:{_PROXY['port']}/sc/{token}/media"
            f"?u={quote(media_url, safe='')}")


def _rewrite(playlist: str, token: str) -> str:
    """Point every absolute URL in a playlist at our own proxy.

    Segments, init segments and key URIs all leave the loopback with
    the CDN headers and no Mouflon token, which is what ffmpeg needs.
    """
    out: list[str] = []
    for line in playlist.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            line = _MAP_URI_RE.sub(
                lambda m: f'URI="{_proxy_url(token, m.group(1))}"'
                if m.group(1).startswith("http") else m.group(0), line)
            out.append(line)
            continue
        out.append(_proxy_url(token, line.strip()))
    return "\n".join(out)


async def _proxy_playlist(request: web.Request) -> web.Response:
    entry = _PROXY["streams"].get(request.match_info["token"])
    if not entry:
        raise web.HTTPNotFound()
    try:
        upstream = await _get_playlist(entry["variant"], entry["referer"])
    except HostError as exc:
        raise web.HTTPBadGateway(text=str(exc))
    if _is_advert(upstream):
        # The advert cannot be recorded as the model; report it instead
        # of letting ffmpeg write a 12-second "recording".
        raise web.HTTPServiceUnavailable(text="advert playlist")
    plain = decode_playlist(upstream, entry["pdkey"])
    return web.Response(
        text=_rewrite(plain, request.match_info["token"]),
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"})


async def _proxy_media(request: web.Request) -> web.Response:
    target = unquote(request.query.get("u", ""))
    if not target.startswith("http") or not _cdn_allowed(target):
        raise web.HTTPForbidden(text="not a stripchat CDN URL")
    entry = next(iter(_PROXY["streams"].values()), {"referer": ""})
    headers = _headers(entry.get("referer", ""))
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    timeout = aiohttp.ClientTimeout(total=None, sock_read=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(target, headers=headers) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    raise web.HTTPBadGateway(text=f"CDN {resp.status}")
                passthrough = {key: resp.headers[key]
                               for key in ("Content-Type", "Content-Range",
                                           "Accept-Ranges", "Content-Length")
                               if key in resp.headers}
                return web.Response(status=resp.status, body=body,
                                    headers=passthrough)
    except web.HTTPException:
        raise
    except Exception as exc:
        raise web.HTTPBadGateway(text=f"CDN fetch failed ({exc})")


async def _ensure_proxy() -> int:
    """Start the loopback proxy once; return its port."""
    if _PROXY["runner"]:
        return _PROXY["port"]
    app = web.Application()
    app.router.add_get("/sc/{token}/live.m3u8", _proxy_playlist)
    app.router.add_get("/sc/{token}/media", _proxy_media)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = _port_open()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    _PROXY.update(runner=runner, port=port)
    return port


async def open_stream(url: str) -> dict:
    """{title, stream_urls, headers} for /live.

    A decrypted live playlist is only valid for the segments it lists,
    so ffmpeg cannot read the CDN directly — it reads the loopback
    proxy, which re-decrypts on every playlist refresh.
    """
    info = await resolve(url)
    if not info.get("encrypted"):
        return {"title": info["title"],
                "stream_urls": [info["media_url"]],
                "headers": _headers(info["referer"])}
    port = await _ensure_proxy()
    token = base64.urlsafe_b64encode(os.urandom(9)).decode().strip("=")
    _PROXY["streams"].clear()  # one recording at a time
    _PROXY["streams"][token] = {"variant": info["media_url"],
                                "referer": info["referer"],
                                "pdkey": info["pdkey"]}
    return {"title": info["title"],
            "stream_urls": [f"http://127.0.0.1:{port}/sc/{token}/live.m3u8"],
            "headers": {}}


async def download(url: str, job_dir: str, progress_cb=None) -> list[str]:
    raise HostError(
        "stripchat is a live site — send the link with /live <link> and "
        "the bot records it into parts instead")
