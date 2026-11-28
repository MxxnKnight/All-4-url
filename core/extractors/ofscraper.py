"""Custom extractor: OnlyFans + Fansly posts via OF-Scraper.

Why this exists: neither yt-dlp nor gallery-dl understands OnlyFans
at all (verified 2026-11-28: yt-dlp 2026.11.23 lists no OnlyFans
extractor, gallery-dl none either), and Fansly is only in gallery-dl,
which needs cookies and cannot decrypt DRM videos. OF-Scraper is the
maintained CLI for both sites (PyPI `ofscraper`, 3.18.2 verified): it
speaks the sites' own APIs, and it decrypts protected videos when a
CDM is configured. Its `manual --url` mode downloads exactly one
post — the bot's paste-a-link flow.

Auth (Space secrets, never in the repo or chat — the owner adds them
in Space Settings, like the Upstash token):
  OnlyFans: OF_SESS, OF_AUTH_ID, OF_USER_AGENT, OF_X_BC
    (sess + auth_id + x-bc + user_agent, copied from the logged-in
    browser; they stop working on logout).
  Fansly:   FANSLY_AUTH_TOKEN, FANSLY_USER_AGENT
    (authorization_token from localStorage `session_active_session`
    -> `token`, plus the browser's user agent).
The module only matches a URL when that site's auth is present, so
without secrets the chain is unchanged: the link falls through to
yt-dlp, gallery-dl and the scrape, exactly as before.

How it runs: each job seeds a throwaway OF-Scraper config dir
(auth.json / fansly_auth.json + a minimal config.json pointing the
download root at the job dir), then runs `ofscraper manual --config
<dir> --site <site> --url <url>` as a subprocess. The config dir lives
outside the job dir (tempfile), so OF-Scraper's own database and
locks never pollute the files the sender collects. Secrets are
written only into that throwaway dir, which is deleted in `finally`.

DRM: protected videos need a CDM (`--key-mode manual` + a private
key / client id). This wrapper does not set one up — without it a
DRM video is left as a `.part` file. When a job produces no complete
file but `.part` files exist, the error says so plainly instead of
pretending the link is broken.

Verified 2026-11-28: `ofscraper` 3.18.2 installs; `manual --url`
accepts a single post URL; a minimal config.json (only
file_options.save_location) + fake auth.json reaches the auth check
(the sandbox's proxy env breaks OF-Scraper's httpx startup — its
Discord handler parses no_proxy's `[::1]` as a URL — so the wrapper
strips every *proxy* variable from the child's environment; the
Space has no proxy vars, so this is a no-op there). NOT verified
live: a real post download — that needs the owner's real auth and
happens on the Space after deploy.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import config
from core import stats
from core.jobs import JobCancelled
from core.utils import list_files

#: Hosts whose POST pages this handles (profiles/creators are NOT
#: matched — a profile URL would scrape a whole timeline, and the bot
#: is a paste-one-link tool).
HOSTS = {
    "onlyfans.com": "onlyfans",
    "www.onlyfans.com": "onlyfans",
    "fansly.com": "fansly",
    "www.fansly.com": "fansly",
}

#: OnlyFans post: /<digits>/<username>. Fansly post: /post/<digits>.
_POST_RE = {
    "onlyfans": re.compile(r"^/(\d+)(?:/([^/]+))?/?$"),
    "fansly": re.compile(r"^/post/(\d+)/?$"),
}

#: OF-Scraper runs are long (a post can hold several videos); the
#: gallery engine allows the same budget.
_TIMEOUT = 900


class OFScraperError(Exception):
    pass


def _of_auth() -> dict | None:
    """OnlyFans auth values from the environment, or None."""
    sess = (config.OF_SESS or "").strip()
    auth_id = (config.OF_AUTH_ID or "").strip()
    user_agent = (config.OF_USER_AGENT or "").strip()
    x_bc = (config.OF_X_BC or "").strip()
    if not (sess and auth_id and user_agent and x_bc):
        return None
    return {
        "sess": sess,
        "auth_id": auth_id,
        "auth_uid": (config.OF_AUTH_UID or "").strip(),
        "user_agent": user_agent,
        "x-bc": x_bc,
    }


def _fansly_auth() -> dict | None:
    """Fansly auth values from the environment, or None."""
    token = (config.FANSLY_AUTH_TOKEN or "").strip()
    user_agent = (config.FANSLY_USER_AGENT or "").strip()
    if not (token and user_agent):
        return None
    if token.lower().startswith("bearer"):
        token = token[len("bearer"):].strip()
    return {
        "authorization_token": token,
        "user_agent": user_agent,
        "check_key": "",
        "device_id": "",
        "device_id_fetched_at": "",
    }


def _site_for_url(url: str) -> str | None:
    """'onlyfans' | 'fansly' for a matching POST url, else None."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url or "")
    except Exception:
        return None
    site = HOSTS.get((parsed.netloc or "").lower())
    if not site:
        return None
    if not _POST_RE[site].match(parsed.path or ""):
        return None
    return site


def _auth_configured(site: str) -> bool:
    return (_of_auth() if site == "onlyfans"
            else _fansly_auth()) is not None


def is_site_url(url: str) -> bool:
    """True only for a post URL on a site whose login is configured.

    The auth gate is the point: without the owner's secrets this
    never matches, so the existing chain is untouched.
    """
    site = _site_for_url(url)
    return site is not None and _auth_configured(site)


def _title(url: str, site: str) -> str:
    from urllib.parse import urlparse

    match = _POST_RE[site].match(urlparse(url).path or "")
    post_id = match.group(1) if match else ""
    name = "OnlyFans" if site == "onlyfans" else "Fansly"
    if site == "onlyfans" and match and match.group(2):
        return f"{name} post {post_id} by {match.group(2)}"
    return f"{name} post {post_id}" if post_id else f"{name} post"


async def resolve(url: str) -> dict:
    """Cheap info for the size check + caption. No network: the real
    work happens in download(), and _custom_flow resolves again there
    through it (the media URL would be stale by then anyway)."""
    site = _site_for_url(url) or "onlyfans"
    return {
        "title": _title(url, site),
        "media_url": "",
        "approx_size": 0,
        "is_playlist": False,
    }


def _seed_config(site: str, job_dir: str) -> str:
    """Throwaway OF-Scraper config dir. Returns its path.

    Layout OF-Scraper expects: <cfg>/config.json (download root) and
    <cfg>/main_profile/auth.json (OnlyFans) or fansly_auth.json
    (Fansly). The download root is the job dir itself; the config
    dir is a sibling temp dir so its database/locks never land in
    the files the sender collects.
    """
    cfgdir = tempfile.mkdtemp(prefix="ofscraper_")
    profile = os.path.join(cfgdir, "main_profile")
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(cfgdir, "config.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"file_options": {"save_location": job_dir}}, handle)
    if site == "onlyfans":
        auth = {"auth": _of_auth()}
        name = "auth.json"
    else:
        auth = _fansly_auth()
        name = "fansly_auth.json"
    with open(os.path.join(profile, name), "w",
              encoding="utf-8") as handle:
        json.dump(auth, handle)
    return cfgdir


def _clean_env() -> dict:
    """Child environment without any proxy variable.

    OF-Scraper's startup (its Discord log handler) builds an httpx
    client from the proxy env, and the sandbox's no_proxy holds
    `[::1]` — httpx parses that as a URL and dies with "Invalid
    port: ':1]'" before any download starts. The Space has no proxy
    variables, so this is a no-op there; it only guards the child
    against a poisoned environment.
    """
    return {k: v for k, v in os.environ.items()
            if "proxy" not in k.lower()}


def _cmd(cfgdir: str, site: str, url: str) -> list[str]:
    """The OF-Scraper invocation. -nl kills the Rich live UI (the
    bot reads plain log lines); --auth-fail quits on a dead login
    instead of prompting (there is no terminal to prompt on)."""
    exe = shutil.which("ofscraper")
    base = [exe] if exe else [sys.executable, "-m", "ofscraper"]
    return base + ["manual", "--config", cfgdir, "--site", site,
                   "--url", url, "-nl", "--auth-fail"]


def _dir_size(root: str) -> int:
    total = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _has_part_files(root: str) -> bool:
    for _dirpath, _dirs, names in os.walk(root):
        if any(n.endswith(".part") for n in names):
            return True
    return False


async def download(url: str, job_dir: str, progress_cb=None,
                   cancel_check=None) -> list[str]:
    site = _site_for_url(url)
    if site is None:
        raise OFScraperError("not an OnlyFans/Fansly post link")
    if not _auth_configured(site):
        name = "OnlyFans" if site == "onlyfans" else "Fansly"
        raise OFScraperError(
            f"{name} login is not set up — add the {name} secrets in "
            "the Space settings and redeploy")

    cfgdir = _seed_config(site, job_dir)
    before = set(list_files(job_dir))
    cmd = _cmd(cfgdir, site, url)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_clean_env(),
    )

    async def _watch():
        """Poll cancel + report bytes landed so far (total unknown —
        the sender shows 'transferred' without a bar, as for any
        no-total download)."""
        last = -1
        while proc.returncode is None:
            if cancel_check is not None and cancel_check():
                proc.kill()
                return
            if progress_cb is not None:
                done = _dir_size(job_dir)
                if done != last:
                    last = done
                    await progress_cb(done, 0)
            await asyncio.sleep(2)

    watch_task = asyncio.create_task(_watch())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(),
                                        timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise OFScraperError(
            "OF-Scraper timed out after 15 minutes")
    finally:
        watch_task.cancel()
        import shutil as _shutil
        _shutil.rmtree(cfgdir, ignore_errors=True)
    if cancel_check is not None and cancel_check():
        raise JobCancelled()

    new_files = [p for p in list_files(job_dir) if p not in before]
    if new_files:
        stats.record_paths("downloaded", new_files)
        return new_files

    tail = (out or b"").decode(errors="replace").strip().splitlines()
    hint = tail[-1][:300] if tail else f"exit code {proc.returncode}"
    low = " ".join(tail).lower()
    if _has_part_files(job_dir):
        raise OFScraperError(
            "that video is DRM-protected — OF-Scraper left only a "
            ".part file. Decrypting it needs a CDM, which isn't set "
            "up on the bot.")
    if "auth" in low or "login" in low or "401" in low or "403" in low:
        name = "OnlyFans" if site == "onlyfans" else "Fansly"
        raise OFScraperError(
            f"{name} refused the login — the saved session may have "
            "expired (logging out kills it). Re-copy the secrets "
            "from the logged-in browser. OF-Scraper said: " + hint)
    raise OFScraperError(hint)
