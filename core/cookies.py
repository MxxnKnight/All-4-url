"""Per-site cookie jars.

One Netscape cookies.txt per platform lives in cookies/<platform>.txt
(cookies/instagram.txt, cookies/reddit.txt, ...). The engines pick the
jar matching the download URL's domain, so refreshing Reddit never
touches Instagram and no site ever sees another site's cookies.

The old single cookies.txt (config.COOKIES_FILE) still works as a
fallback when no jar matches.
"""

import os
import tempfile
import time
from urllib.parse import urlparse

import config

#: (platform id, button label, domains that identify it)
COOKIE_SITES = [
    ("youtube", "YouTube", ("youtube.com", "youtu.be")),
    ("instagram", "Instagram", ("instagram.com",)),
    ("reddit", "Reddit", ("reddit.com",)),
    ("x", "X (Twitter)", ("x.com", "twitter.com")),
    ("tiktok", "TikTok", ("tiktok.com",)),
    ("facebook", "Facebook", ("facebook.com", "fb.com")),
]

COOKIES_DIR = os.path.join(config.BASE_DIR, "cookies")

#: Cookie names that prove a *logged-in* session (not just a visitor).
#: Used to warn when an installed file probably won't unblock the site.
LOGIN_MARKERS = {
    "youtube": ("SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO"),
    "instagram": ("sessionid", "ds_user_id"),
    "reddit": ("reddit_session",),
    "x": ("auth_token",),
    "tiktok": ("sessionid",),
    "facebook": ("c_user", "xs"),
}

#: message ids already consumed as cookie installs, so file-to-link
#: (group 0) skips files the cookie handler (group -1) claimed.
CONSUMED: set = set()

#: owner_id -> (platform | "auto", timestamp). Set when the owner taps a
#: /setcookies menu button; the next document they send is that jar.
PENDING: dict = {}
PENDING_TTL = 600


def site_ids() -> set:
    return {site[0] for site in COOKIE_SITES}


def site_label(platform: str) -> str:
    for pid, label, _ in COOKIE_SITES:
        if pid == platform:
            return label
    return platform


def jar_path(platform: str) -> str:
    safe = "".join(c for c in platform if c.isalnum() or c in "-_") or "other"
    return os.path.join(COOKIES_DIR, f"{safe}.txt")


def parse_cookies(text: str) -> tuple:
    """(cookie_count, sorted domains) for Netscape-format text."""
    domains, count = set(), 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            count += 1
            domains.add(parts[0].lstrip(".").lower())
    return count, sorted(domains)


def cookie_names(text: str) -> set:
    """Cookie names present in Netscape-format text."""
    names = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            names.add(parts[5])
    return names


def looks_logged_in(platform: str, text: str) -> bool:
    """Does the file contain a logged-in session cookie for the site?"""
    markers = LOGIN_MARKERS.get(platform)
    if not markers:
        return True  # unknown site — nothing to check against
    names = cookie_names(text)
    return any(m in names for m in markers)


def domain_matches(cookie_domain: str, site_domains: tuple) -> bool:
    cookie_domain = cookie_domain.lower()
    return any(cookie_domain == d or cookie_domain.endswith("." + d)
               for d in site_domains)


def detect_platform(domains: list) -> str | None:
    """Best-guess platform id from the cookie domains in a file."""
    for pid, _label, site_domains in COOKIE_SITES:
        if any(domain_matches(d, site_domains) for d in domains):
            return pid
    return None


def platform_for_url(url: str) -> str | None:
    """Platform id for a download URL, or None."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    for pid, _label, site_domains in COOKIE_SITES:
        if domain_matches(host, site_domains):
            return pid
    return None


def jar_for_url(url: str) -> str | None:
    """Cookie file the engines should use for this URL, if any."""
    platform = platform_for_url(url)
    if platform:
        path = jar_path(platform)
        if os.path.isfile(path):
            return path
    legacy = config.cookies_path()
    return legacy  # old single cookies.txt, or None


def install_jar(platform: str, text: str) -> tuple:
    """Atomically (re)write a jar. Returns (cookie_count, domains)."""
    count, domains = parse_cookies(text)
    if count == 0:
        raise ValueError("no cookie lines found")
    dest = jar_path(platform)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest),
                               prefix=".cookies", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, dest)
    return count, domains


def list_jars() -> list:
    """[(platform, label, count, installed_ts)] for jars on disk."""
    jars = []
    if not os.path.isdir(COOKIES_DIR):
        return jars
    for pid, label, _ in COOKIE_SITES:
        path = jar_path(pid)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    count, _domains = parse_cookies(fh.read(2_000_000))
            except OSError:
                count = 0
            jars.append((pid, label, count, os.path.getmtime(path)))
    # jars we don't have buttons for (e.g. auto-detected odd domains)
    known = {jar_path(pid) for pid, _l, _d in COOKIE_SITES}
    for name in sorted(os.listdir(COOKIES_DIR)):
        if not name.endswith(".txt"):
            continue
        path = os.path.join(COOKIES_DIR, name)
        if path in known or not os.path.isfile(path):
            continue
        pid = name[:-4]
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                count, _domains = parse_cookies(fh.read(2_000_000))
        except OSError:
            count = 0
        jars.append((pid, pid, count, os.path.getmtime(path)))
    return jars


def set_pending(owner_id: int, platform: str) -> None:
    PENDING[owner_id] = (platform, time.time())


def take_pending(owner_id: int):
    """(platform | None). Expired entries are dropped."""
    entry = PENDING.pop(owner_id, None)
    if not entry:
        return None
    platform, ts = entry
    if time.time() - ts > PENDING_TTL:
        return None
    return platform


def clear_pending(owner_id: int) -> None:
    PENDING.pop(owner_id, None)


def fallback_platform(domains: list) -> str:
    """Jar id for cookie domains we have no button for.

    Uses the registrable part of the first domain, e.g.
    ["sub.example.com"] -> "example".
    """
    if not domains:
        return "other"
    parts = domains[0].split(".")
    base = parts[-2] if len(parts) >= 2 else parts[0]
    safe = "".join(c for c in base if c.isalnum() or c in "-_")
    return safe or "other"
