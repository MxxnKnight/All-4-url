"""Shared, browser-free helpers for the custom site extractors.

Everything here is plain HTTP. A headless browser cannot run on the
Koyeb free tier (0.1 vCPU / 512 MB), so each custom extractor reads the
site's own HTML and JavaScript and replays only the small bits of
client-side logic the page needs:

  * packed (Dean Edwards) ``eval()`` blocks,
  * "obfuscated" URLs built by concatenating and sub-stringing
    literals (``'//host/get_video?i' + ('xcdd=...').substring(1)``),
  * download-gate ``<form>`` POSTs and meta/location redirects.

Nothing in here executes remote JavaScript; the functions only parse
and re-implement well-defined string handling, so a hostile page can
never run code inside the bot.
"""

import asyncio
import base64
import binascii
import html as html_lib
import json
import re
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from core.extractors.direct import BROWSER_UA, is_playlist_url


class HostError(Exception):
    """A custom extractor could not produce a usable media URL."""


#: Markers of Cloudflare's interactive "Just a moment…" browser check.
CF_MARKERS = (
    "just a moment",
    "cf-challenge",
    "challenge-platform",
    "/cdn-cgi/challenge",
    "checking your browser",
)


def looks_challenged(status: int, body: str) -> bool:
    if status not in (403, 429, 503):
        return False
    low = (body or "").lower()
    return any(marker in low for marker in CF_MARKERS)


def _default_headers() -> dict:
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


# ------------------------------------------------------------------ fetch

def _curl_cffi_sync(url: str, headers: dict, data, method: str | None,
                    params=None):
    """One request through curl_cffi (passes Cloudflare's TLS checks)."""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return None
    try:
        resp = creq.request(method or ("POST" if data is not None else "GET"),
                            url, headers=headers, data=data, params=params,
                            impersonate="chrome", timeout=35,
                            allow_redirects=True)
        return str(resp.url), resp.text, int(resp.status_code)
    except Exception:
        return None


async def _curl_cffi(url: str, headers: dict, data, method: str | None,
                     params=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _curl_cffi_sync, url, headers,
                                      data, method, params)


async def request(session: aiohttp.ClientSession, url: str,
                  method: str | None = None, data=None, headers: dict | None = None,
                  params=None):
    """Fetch one page inside an existing session.

    Returns ``(final_url, text, status)``. A Cloudflare *TLS* check is
    retried once through curl_cffi (same cookies are lost on that retry,
    so it is only a rescue for stateless pages). A real interactive
    browser challenge is reported as an honest error — the bot cannot
    solve it without a browser.

    `params` are query-string fields (what a GET form submits); `data`
    is the request body (what a POST form submits). They are different
    things — a GET form's fields sent as `data` arrive in the body,
    which servers ignore, so the gate silently never opens.
    """
    hdrs = _default_headers()
    if headers:
        hdrs.update(headers)
    method = method or ("POST" if data is not None else "GET")
    try:
        async with session.request(method, url, headers=hdrs, data=data,
                                   params=params,
                                   allow_redirects=True) as resp:
            status = resp.status
            final = str(resp.url)
            body = await resp.text(errors="replace")
    except Exception as exc:
        raise HostError(f"couldn't reach the site ({exc})") from exc
    if status >= 400 and looks_challenged(status, body):
        alt = await _curl_cffi(url, hdrs, data, method, params)
        if alt and alt[2] < 400:
            return alt
        raise HostError(
            "the site is behind a Cloudflare browser check — it cannot be "
            "passed without a real browser")
    return final, body, status


async def fetch(url: str, method: str | None = None, data=None,
                headers: dict | None = None):
    """Standalone fetch (its own throw-away session). See request()."""
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await request(session, url, method=method, data=data,
                             headers=headers)


async def fetch_page(url: str, headers: dict | None = None) -> tuple[str, str]:
    """Fetch a page that must exist; returns (final_url, html)."""
    final, body, status = await fetch(url, headers=headers)
    if status >= 400:
        if status == 404:
            raise HostError("the file page returned 404 — the link is dead "
                            "or the file was removed")
        raise HostError(f"the file page returned HTTP {status}")
    return final, body


# ------------------------------------------------------------- packed JS

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
    """Unpack a Dean Edwards packed ``eval(function(p,a,c,k,e,d){...})``."""
    match = _PACKER_RE.search(js or "")
    if not match:
        return None
    payload, base, count = (match.group(1), int(match.group(2)),
                            int(match.group(3)))
    words = match.group(4).split("|")
    out = payload
    for i in range(count - 1, -1, -1):
        if i < len(words) and words[i]:
            out = re.sub(r"\b" + _base_n(i, base) + r"\b", words[i], out)
    return out


def scripts(page: str) -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", page or "", re.S)


# ------------------------------------------------- JS string expressions

_LITERAL_RE = re.compile(r"""^(?:\()?\s*('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")
_METHOD_RE = re.compile(
    r"""\.(substring|substr|slice|replace|toLowerCase|toUpperCase|trim)\(([^)]*)\)""")


def _split_top(expr: str, sep: str = "+") -> list[str]:
    """Split on `sep` outside quotes and parentheses."""
    parts, buf, depth, quote = [], [], 0, ""
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(expr):
                buf.append(expr[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] in ("'", '"') and token[-1] == token[0]:
        return token[1:-1].replace("\\" + token[0], token[0]).replace("\\\\", "\\")
    return token


def eval_js_string(expr: str) -> str | None:
    """Evaluate the tiny JS subset sites use to hide a URL.

    Handles string literals joined by ``+`` with ``.substring(n[,m])``,
    ``.substr(n[,m])``, ``.slice(n[,m])``, ``.replace(a,b)``,
    ``.toUpperCase()``, ``.toLowerCase()`` and ``.trim()`` applied to
    them — exactly the shapes seen on streamtape-style players.

    Returns None when the expression uses anything outside that subset,
    so the caller can fall back instead of guessing.
    """
    out: list[str] = []
    for segment in _split_top(expr):
        segment = segment.strip()
        if not segment:
            continue
        match = _LITERAL_RE.match(segment)
        if not match:
            return None
        value = _unquote(match.group(1))
        rest = segment[match.end():]
        for name, raw_args in _METHOD_RE.findall(rest):
            argv = [_unquote(a) for a in _split_top(raw_args, ",")]
            try:
                if name in ("substring", "substr", "slice"):
                    nums = [int(a) for a in argv if a.strip() != ""]
                    if name == "substr" and len(nums) == 2:
                        nums[1] = nums[0] + nums[1]
                    start = nums[0] if nums else 0
                    end = nums[1] if len(nums) > 1 else None
                    value = value[start:end]
                elif name == "replace":
                    if len(argv) < 2:
                        return None
                    value = value.replace(argv[0], argv[1])
                elif name == "toLowerCase":
                    value = value.lower()
                elif name == "toUpperCase":
                    value = value.upper()
                elif name == "trim":
                    value = value.strip()
            except (ValueError, TypeError):
                return None
        out.append(value)
    if not out:
        return None
    return "".join(out)


def normalise_url(found: str, base_url: str) -> str:
    """Make a URL absolute: protocol-relative, root-relative, or plain."""
    found = html_lib.unescape((found or "").strip()).replace("\\/", "/")
    parsed = urlparse(base_url)
    scheme = parsed.scheme or "https"
    if found.startswith("//"):
        return f"{scheme}:{found}"
    if found.startswith("/"):
        return f"{scheme}://{parsed.netloc}{found}"
    if found.startswith("http://") or found.startswith("https://"):
        return found
    if found.startswith("./"):
        found = found[2:]
    return urljoin(base_url, found)


# ---------------------------------------------------------- media scanner

MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".m3u8", ".ts",
              ".flv", ".mp3", ".m4a")

#: Never mistake player libraries, posters or ad slots for the video.
_SKIP_HINTS = (
    "jwplayer", "player.js", "hls.js", "hls.min", "preview", "thumb",
    "poster", "sprite", "doubleclick", "googlesyndication", "adsystem",
    "banner", "pixel", ".gif", ".css", ".js", ".woff",
)

#: Ordered by confidence — the player's own config first, page meta last.
_MEDIA_PATTERNS = (
    r'"streaming_url"\s*:\s*"([^"]+)"',
    r'"streamingUrl"\s*:\s*"([^"]+)"',
    r'"hls(?:_url|Url|_link)"\s*:\s*"([^"]+)"',
    r'"playlist"\s*:\s*"([^"]+)"',
    r'sources\s*:\s*\[\s*\{[^}]*?file\s*:\s*["\']([^"\']+)["\']',
    r'file\s*:\s*["\']([^"\']+\.(?:mp4|m3u8|mkv|webm|ts)[^"\']*)["\']',
    r'["\']file["\']\s*:\s*["\']([^"\']+\.(?:mp4|m3u8|mkv|webm|ts)[^"\']*)["\']',
    r'["\']src["\']\s*:\s*["\']([^"\']+\.(?:mp4|m3u8|mkv|webm|ts)[^"\']*)["\']',
    r'<source[^>]+src=["\']([^"\']+)["\']',
    r'<video[^>]+src=["\']([^"\']+)["\']',
    r'<video[^>]+data-[a-z-]*src=["\']([^"\']+)["\']',
    r'''property=["\']og:video(?::secure_url|:url)?["\']\s+content=["\']([^"\']+)''',
    r'''content=["\']([^"\']+)["\']\s+property=["\']og:video(?::secure_url|:url)?''',
    r'''name=["\']twitter:player:stream["\']\s+content=["\']([^"\']+)''',
    r'(https?://[^\s"\'<>\\]+\.m3u8(?:\?[^\s"\'<>\\]*)?)',
    r'(https?://[^\s"\'<>\\]+\.(?:mp4|mkv|webm)(?:\?[^\s"\'<>\\]*)?)',
    r'["\']([^"\']+\.(?:mp4|mkv|m3u8)(?:\?[^"\']*)?)["\']',
)

#: <img> sources for image hosts (imagetwist & friends).
_IMAGE_PATTERNS = (
    r'<img[^>]+class=["\'][^"\']*(?:centred|centered|main)[^"\']*["\'][^>]+src=["\']([^"\']+)',
    r'<img[^>]+src=["\'](https?://[^"\']+/i/[^"\']+\.(?:jpg|jpeg|png|gif|webp)[^"\']*)',
    r'property=["\']og:image["\']\s+content=["\']([^"\']+)',
)


def _plausible(found: str, *, images: bool = False) -> bool:
    low = found.lower()
    if not found or low.startswith(("data:", "blob:", "javascript:")):
        return False
    if images:
        # When the image itself is the prize, "poster"/"thumb" hints
        # must not disqualify it — only ads and code files are skipped.
        hints = ("jwplayer", "player.js", "hls.js", "hls.min",
                 "doubleclick", "googlesyndication", "adsystem",
                 "banner", "pixel", ".css", ".js", ".woff")
    else:
        hints = _SKIP_HINTS
    if any(hint in low for hint in hints):
        return False
    return True


def scan_media(text: str, base_url: str, *, images: bool = False) -> str | None:
    """First believable media URL in `text` (already unpacked), or None."""
    if not text:
        return None
    for pattern in _MEDIA_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            found = normalise_url(match.group(1), base_url)
            if _plausible(found):
                return found
    if images:
        for pattern in _IMAGE_PATTERNS:
            for match in re.finditer(pattern, text, re.I):
                found = normalise_url(match.group(1), base_url)
                if _plausible(found, images=True):
                    return found
    return None


def find_media(page: str, base_url: str, *, images: bool = False) -> str | None:
    """Media URL from a page, trying the raw HTML then unpacked scripts."""
    found = scan_media(page, base_url, images=images)
    if found:
        return found
    # Client-rendered apps (Next.js, Vue) ship the same JSON with escaped
    # quotes/slashes: \"url\":\"https:\/\/host\/video.mp4\". Scan that too.
    unescaped = None
    if '\\"' in page or "\\/" in page:
        unescaped = page.replace('\\"', '"').replace("\\/", "/")
        found = scan_media(unescaped, base_url, images=images)
        if found:
            return found
    found = scan_json_media(page, base_url, images=images)
    if found:
        return found
    if unescaped:
        found = scan_json_media(unescaped, base_url, images=images)
        if found:
            return found
    # srcdoc iframes carry the player document inline — scan it directly.
    for doc in re.findall(r'<iframe[^>]+srcdoc=(["\'])(.*?)\1',
                          page or "", re.S | re.I):
        doc = html_lib.unescape(doc[1])
        if "<" not in doc:
            continue
        found = scan_media(doc, base_url, images=images)
        if not found:
            found = scan_json_media(doc, base_url, images=images)
        if found:
            return found
    for body in scripts(page):
        unpacked = unpack_packed(body)
        if unpacked:
            found = scan_media(unpacked, base_url, images=images)
            if found:
                return found
    return None


# ------------------------------------------------- JSON config scanning
# Ghost/CatCatch's idea, done statically: players bury their stream URL
# in a JSON config ({"sources": [{"file": "..."}]}) where a flat regex
# can miss it — the URL may have no media extension at all (tokenised
# stream links), with only the key name saying what it is.

#: Keys whose value is the media itself, even with no file extension.
_MEDIA_KEYS = (
    "file", "source", "hls", "m3u8", "mp4", "stream", "playbackurl",
    "videourl", "streamurl", "downloadurl", "contenturl",
)

#: Same idea as a flat regex, for configs that are not valid JSON
#: (JS object literals with trailing commas, single quotes, ...).
_KEY_HINT_RE = re.compile(
    r"""["']?(file|source|hls|m3u8|mp4|stream|playbackUrl|videoUrl|streamUrl|downloadUrl|contentUrl)["']?\s*:\s*["'](https?://[^"'\s<>\\]+)["']""",
    re.I)

_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>',
    re.S | re.I)

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _json_media_walk(node, base_url: str, *, images: bool = False,
                     depth: int = 0, budget: list | None = None) -> str | None:
    """Walk a parsed JSON value looking for a media URL.

    A string value counts when its path ends with a media extension, or
    when its key names it as media (a tokenised stream link has no
    extension to match on). `budget` caps the nodes visited so a huge
    config cannot burn CPU on the free tier.
    """
    if budget is None:
        budget = [2000]
    if budget[0] <= 0 or depth > 8:
        return None
    budget[0] -= 1
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value.startswith(
                    ("http://", "https://", "//")):
                url = normalise_url(value, base_url)
                path = urlparse(url).path.lower()
                if _plausible(url):
                    if any(path.endswith(ext)
                           for ext in MEDIA_EXTS + (".mpd",)):
                        return url
                    if str(key).lower() in _MEDIA_KEYS:
                        return url
                if (images and _plausible(url, images=True)
                        and any(path.endswith(ext) for ext in _IMAGE_EXTS)):
                    return url
            elif isinstance(value, (dict, list)):
                found = _json_media_walk(value, base_url, images=images,
                                         depth=depth + 1, budget=budget)
                if found:
                    return found
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                found = _json_media_walk(item, base_url, images=images,
                                         depth=depth + 1, budget=budget)
                if found:
                    return found
            elif isinstance(item, str) and item.startswith(
                    ("http://", "https://", "//")):
                url = normalise_url(item, base_url)
                path = urlparse(url).path.lower()
                if (_plausible(url) and any(
                        path.endswith(ext) for ext in MEDIA_EXTS + (".mpd",))):
                    return url
                if (images and _plausible(url, images=True) and any(
                        path.endswith(ext) for ext in _IMAGE_EXTS)):
                    return url
    return None


def scan_json_media(page: str, base_url: str, *,
                    images: bool = False) -> str | None:
    """Media URL buried in the page's JSON configs, or None."""
    if not page:
        return None
    for body in _JSON_SCRIPT_RE.findall(page):
        try:
            data = json.loads(html_lib.unescape(body.strip()))
        except (ValueError, TypeError):
            continue
        found = _json_media_walk(data, base_url, images=images)
        if found:
            return found
    for match in _KEY_HINT_RE.finditer(page):
        url = normalise_url(match.group(2), base_url)
        if _plausible(url):
            return url
    return None


# ------------------------------------------------- data: playlist URIs
# Some players embed the whole HLS playlist as a data: URI instead of
# a link (Ghost decodes these in the browser). The bot can decode the
# same text and hand it to ffmpeg — no browser needed.

_DATA_URI_RE = re.compile(r"data:([^,;]+)(;base64)?,([A-Za-z0-9+/=_%\-.]+)",
                          re.I)


def extract_data_playlist(page: str) -> str | None:
    """Decoded ``#EXTM3U`` text from a data: playlist URI, or None."""
    if not page or "data:" not in page:
        return None
    for mime, b64, payload in _DATA_URI_RE.findall(page):
        if "mpegurl" not in mime.lower():
            continue
        if len(payload) > 200_000:
            continue
        try:
            if b64:
                raw = base64.b64decode(payload + "=" * (-len(payload) % 4))
                text = raw.decode("utf-8", "replace")
            else:
                text = unquote(payload)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            continue
        text = text.strip().lstrip("\ufeff")
        if text[:7].upper() == "#EXTM3U":
            return text
    return None


def resolve_playlist_urls(text: str, base_url: str) -> str:
    """Rewrite a playlist's relative segment/key URLs against `base_url`."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if "URI=" in line:
                def _abs(match):
                    uri = match.group(1)
                    if uri.startswith(("http://", "https://", "data:")):
                        return match.group(0)
                    return f'URI="{urljoin(base_url, uri)}"'
                line = re.sub(r'URI="([^"]+)"', _abs, line)
            out.append(line)
            continue
        if line.startswith(("http://", "https://", "data:")):
            out.append(line)
        else:
            out.append(urljoin(base_url, line))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------ iframes
# The player is often a separate document: the page holds an <iframe>
# and the video link lives inside the framed page, which the bot never
# fetched. These helpers find the frames worth opening (at most 3, ads
# and social widgets skipped — each fetch costs time on the free tier).

_IFRAME_BLOCK = (
    "googlesyndication", "doubleclick", "googleadservices",
    "googletagmanager", "facebook.com/plugins", "platform.twitter",
    "youtube.com/embed", "player.vimeo.com", "tiktok.com/embed",
    "instagram.com/embed",
)

_IFRAME_SRC_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)


def iframe_srcs(page: str, base_url: str, *, limit: int = 3) -> list[str]:
    """Player-ish <iframe> URLs from a page, most promising first."""
    if not page:
        return []
    found: list[str] = []
    for raw in _IFRAME_SRC_RE.findall(page):
        raw = (raw or "").strip()
        if not raw or raw.startswith(
                ("javascript:", "data:", "about:", "blob:", "#")):
            continue
        url = normalise_url(raw, base_url)
        if not url.startswith(("http://", "https://")):
            continue
        low = url.lower()
        if any(host in low for host in _IFRAME_BLOCK):
            continue
        if url not in found:
            found.append(url)

    def _player_first(url: str) -> int:
        low = url.lower()
        return 0 if any(h in low for h in
                        ("player", "embed", "video", "stream", "watch")) else 1

    found.sort(key=_player_first)
    return found[:limit]


# ------------------------------------------------------------ page pieces

_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S | re.I)
_INPUT_RE = re.compile(r"<input\b([^>]*)/?>", re.I | re.S)
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def attrs(tag: str) -> dict:
    out = {}
    for name, dq, sq, bare in _ATTR_RE.findall(tag or ""):
        out[name.lower()] = html_lib.unescape(dq or sq or bare or "")
    return out


def page_title(page: str) -> str:
    for pattern in (r'property=["\']og:title["\']\s+content=["\']([^"\']+)',
                    r"<title[^>]*>([^<]+)</title>"):
        match = re.search(pattern, page or "", re.I)
        if match:
            title = html_lib.unescape(match.group(1)).strip()
            title = re.sub(r"\s+", " ", title)
            # Drop the site's own suffix: "Movie.mkv at Streamtape.com".
            title = re.sub(r"\s+(?:at|on|-|—|\|)\s+[A-Za-z0-9.-]+\.(?:com|net|to|io|so|cc|us|co|org|xyz|to)\s*$",
                           "", title, flags=re.I)
            return title.strip() or "video"
    return "video"


#: Field names that mark a form as a download gate, not a search box.
_GATE_FIELDS = {
    "op", "id", "rand", "referer", "method_free", "method_premium",
    "submitted", "k", "d", "sess_id", "utype", "token", "_token", "csrf",
    "csrf_token", "_csrf", "step", "file_code", "filecode", "down_direct",
    "download", "download_type", "submit", "hash", "nonce", "auth", "src",
}


def forms(page: str) -> list[dict]:
    """Every <form> as {action, method, fields{name: value}, id, text}.

    `id` is the form's id attribute and `text` its visible text
    (tags stripped, lowercased) — gate_form uses both to tell the
    download form apart from unrelated forms on the same page."""
    out = []
    for raw_attrs, inner in _FORM_RE.findall(page or ""):
        attributes = attrs(raw_attrs)
        fields = {}
        for raw_input in _INPUT_RE.findall(inner):
            field = attrs(raw_input)
            name = field.get("name")
            if not name:
                continue
            # Submit inputs are kept: file hosts carry the button's value
            # as data (XFileSharing's method_free=Free Download).
            fields[name] = field.get("value", "")
        text = re.sub(r"<[^>]+>", " ", inner or "")
        text = re.sub(r"\s+", " ", html_lib.unescape(text)).strip().lower()
        out.append({
            "action": attributes.get("action", ""),
            "method": (attributes.get("method") or "get").lower(),
            "fields": fields,
            "id": (attributes.get("id") or "").lower(),
            "text": text,
        })
    return out


def gate_form(page: str, base_url: str) -> dict | None:
    """The download-gate form on a page, if it has one.

    A page often carries several POST forms that all share one gate
    field (a Laravel page's locale switcher and its download form
    both carry only _token) — scoring on gate fields alone ties,
    and the first form in the HTML wins the tie even when it is not
    the download form (proven on a real UpFiles page, 2026-11-13:
    the walker picked the locale form and would have looped on it).
    So download signals break the tie: a download id, a download
    field name, a download button label, and posting back to the
    page itself, which is what download gates do.
    """
    best = None
    for form in forms(page):
        names = {name.lower() for name in form["fields"]}
        if not names & _GATE_FIELDS:
            continue
        action = normalise_url(form["action"] or base_url, base_url)
        form["action"] = action
        # Prefer POST gates: that is what file hosts use.
        score = len(names & _GATE_FIELDS) + (2 if form["method"] == "post" else 0)
        if "download" in form["id"]:
            score += 3
        if any("download" in name for name in names):
            score += 3
        if "download" in form["text"]:
            score += 2
        if action.rstrip("/") == base_url.rstrip("/"):
            score += 2
        if best is None or score > best[0]:
            best = (score, form)
    return best[1] if best else None


_REDIRECT_PATTERNS = (
    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*?url=([^"\'>\s]+)',
    r'location\.(?:href|replace)\s*=\s*["\']([^"\']+)["\']',
    r'location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
    r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
    r'top\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
    r'setTimeout\(\s*(?:function\s*\(\)\s*\{\s*)?(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
)


def next_page(page: str, base_url: str, current: str) -> str | None:
    """A client-side redirect target on the page (meta refresh / JS)."""
    for pattern in _REDIRECT_PATTERNS:
        match = re.search(pattern, page or "", re.I)
        if match:
            target = normalise_url(match.group(1), base_url)
            if target and target != current:
                return target
    return None


_LINK_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S | re.I)
_DOWNLOAD_HINTS = ("download", "/dl", "/d/", "/get", "/stream", "/file/",
                   "direct", "mirror")


def download_links(page: str, base_url: str) -> list[str]:
    """<a> links that look like they hand out the file itself."""
    found: list[str] = []
    for raw_attrs, inner in _LINK_RE.findall(page or ""):
        attributes = attrs(raw_attrs)
        href = attributes.get("href") or attributes.get("data-href") or ""
        if not href:
            continue
        label = re.sub(r"<[^>]+>", " ", inner).strip().lower()
        low = href.lower()
        looks_right = (
            any(low.split("?")[0].endswith(ext) for ext in MEDIA_EXTS)
            or any(hint in low for hint in _DOWNLOAD_HINTS)
            or any(hint in label for hint in _DOWNLOAD_HINTS)
        )
        if not looks_right:
            continue
        absolute = normalise_url(href, base_url)
        if absolute.startswith("http") and absolute not in found:
            found.append(absolute)
    return found[:6]


def media_kind(url: str) -> str:
    """'hls' | 'file' | 'page' from a URL's path."""
    path = urlparse(url).path.lower()
    if is_playlist_url(url):
        return "hls"
    if any(path.endswith(ext) for ext in MEDIA_EXTS):
        return "file"
    return "page"
