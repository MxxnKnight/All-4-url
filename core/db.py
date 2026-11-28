"""Upstash Redis storage (optional, recommended).

Enabled when UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are
both set. The bot talks to Redis over Upstash's REST API — plain
HTTPS on port 443 — which is why this works from HF at all: the old
MongoDB backend (removed in v1.51.17) needed port 27017, which HF's
egress blocks, so it could never connect from the Space.

Everything lives under four JSON keys (one GET/SET each, no KEYS or
SCAN — the whole database is a handful of keys):

    a4u:sudo     [123, 456]                    — /addsudo users
    a4u:usage    {"total": {...}, "2026-11": {...}, "2026-11-28": {...}}
                                               — bandwidth + jobs
    a4u:cookies  {"youtube": "<netscape file>", ...} — cookie jars
    a4u:settings {"send_as": ..., ...}          — bot settings

The owner knowingly stores cookie jars here (throwaway accounts only,
never main accounts) so they survive redeploys; the backup vault
(core/backup.py) keeps a second copy. Anyone with the REST token
could use those sessions — treat the token like a password.

If the env vars are unset or Redis is unreachable, the bot falls
back to the local JSON files exactly as before — the database is an
upgrade, never a single point of failure. Usage increments are
read-modify-write (the REST API has no $inc); the bot is a single
process and a lost racing increment on a counter is harmless.

Free-tier headroom: the bot uses ~4 keys and a few hundred commands
a day at most, against 500K commands/month and 250 MB.
"""

import asyncio
import json
import logging

import aiohttp

import config

log = logging.getLogger("all4url")

_TIMEOUT = aiohttp.ClientTimeout(total=10)

_MISSING = object()
_enabled = False

# Key layout (see module docstring).
_K_SUDO = "a4u:sudo"
_K_USAGE = "a4u:usage"
_K_COOKIES = "a4u:cookies"
_K_SETTINGS = "a4u:settings"


def enabled() -> bool:
    return _enabled


def _rest_host() -> str:
    """Host part of the REST URL for logs — never the token."""
    try:
        rest = config.UPSTASH_REDIS_REST_URL.split("://", 1)[-1]
        return rest.split("/")[0] or "?"
    except Exception:
        return "?"


async def _http(method: str, path: str, body: str | None = None):
    """One REST call. Returns (True, result) or (False, None).

    Never raises: any network error, timeout, non-200 status or
    Upstash {"error": ...} body is a plain failure, and the callers
    fall back to the local files.
    """
    url = config.UPSTASH_REDIS_REST_URL.rstrip("/") + path
    headers = {"Authorization": "Bearer " + config.UPSTASH_REDIS_REST_TOKEN}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.request(method, url, headers=headers,
                                       data=body) as resp:
                if resp.status != 200:
                    return False, None
                data = await resp.json()
    except Exception as exc:
        log.warning("upstash %s %s failed: %s", method, path, exc)
        return False, None
    if isinstance(data, dict) and "error" in data:
        log.warning("upstash %s %s refused: %s", method, path,
                    data.get("error"))
        return False, None
    if not isinstance(data, dict) or "result" not in data:
        return False, None
    return True, data["result"]


async def _get_json(key: str):
    """Parsed JSON at `key`: _MISSING when the key is absent,
    None on transport error, else the value."""
    ok, result = await _http("GET", "/get/" + key)
    if not ok:
        return None
    if result is None:
        return _MISSING
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


async def _set_json(key: str, value) -> bool:
    """SET `key` to the JSON encoding of `value` (POST body, so big
    values like cookie jars never travel in the URL)."""
    ok, result = await _http("POST", "/set/" + key,
                             body=json.dumps(value))
    return bool(ok and result == "OK")


async def init() -> bool:
    """Ping the database. Returns True when Redis is usable."""
    global _enabled
    if not config.UPSTASH_REDIS_REST_URL or not config.UPSTASH_REDIS_REST_TOKEN:
        log.info("UPSTASH_REDIS_REST_URL/TOKEN not set — local JSON "
                 "storage (Telegram backups ON)")
        return False
    log.info("Upstash Redis set (host: %s) — connecting…",
             _rest_host())
    ok, result = await _http("GET", "/ping")
    if ok and result == "PONG":
        _enabled = True
        log.info("✅ Upstash Redis connected — sudo/usage/cookies/"
                 "settings live in Redis; Telegram backups OFF")
        return True
    log.warning("⚠️ Upstash Redis NOT connected — falling back to "
                "local files; Telegram backups ON.")
    _enabled = False
    return False


def schedule(coro) -> None:
    """Fire-and-forget an async DB write from sync code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()  # no loop (startup/tests) — next load reconciles
        return
    task = loop.create_task(coro)
    task.add_done_callback(_swallow)


def _swallow(task) -> None:
    if not task.cancelled() and task.exception():
        log.warning("background db write failed: %s", task.exception())


# --- sudo ---------------------------------------------------------------

async def load_sudo_ids():
    """Runtime sudo ids from Redis, or None when unavailable."""
    if not _enabled:
        return None
    value = await _get_json(_K_SUDO)
    if value is None:
        return None
    if value is _MISSING:
        return set()
    try:
        return {int(x) for x in value}
    except (TypeError, ValueError):
        return set()


async def save_sudo_ids(ids) -> None:
    if not _enabled:
        return
    try:
        await _set_json(_K_SUDO, sorted(int(x) for x in ids))
    except Exception as exc:
        log.warning("upstash sudo save failed: %s", exc)


# --- usage ---------------------------------------------------------------

async def load_usage():
    """All usage buckets as {bucket_id: doc}, or None when unavailable.
    An empty database returns {} (the caller migrates the local file
    up, exactly like the old Mongo backend did)."""
    if not _enabled:
        return None
    value = await _get_json(_K_USAGE)
    if value is None:
        return None
    if value is _MISSING:
        return {}
    return value if isinstance(value, dict) else {}


def _inc_path(doc: dict, path: str, n) -> None:
    """Add `n` at a dotted field path, creating parents as needed."""
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    try:
        cur[parts[-1]] = int(cur.get(parts[-1]) or 0) + int(n)
    except (TypeError, ValueError):
        pass


async def inc_usage(buckets: dict) -> bool:
    """Add several usage increments. buckets maps bucket id -> flat
    field paths ({"downloaded": n, "jobs.uploader.ok": n}).
    True if written."""
    if not _enabled or not buckets:
        return False
    try:
        value = await _get_json(_K_USAGE)
        if value is None:
            return False
        data = value if isinstance(value, dict) else {}
        for bucket, fields in buckets.items():
            if not fields:
                continue
            period = data.get(bucket)
            if not isinstance(period, dict):
                period = {}
                data[bucket] = period
            for path, n in fields.items():
                _inc_path(period, path, n)
        return await _set_json(_K_USAGE, data)
    except Exception as exc:
        log.warning("upstash usage write failed: %s", exc)
        return False


# --- cookie jars -----------------------------------------------------------

async def load_cookies():
    """{platform: netscape_text} from Redis, or None when unavailable."""
    if not _enabled:
        return None
    value = await _get_json(_K_COOKIES)
    if value is None:
        return None
    if value is _MISSING:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()
            if isinstance(v, str) and v}


async def save_cookie(platform: str, text: str) -> None:
    if not _enabled:
        return
    try:
        value = await _get_json(_K_COOKIES)
        if value is None:
            return
        data = value if isinstance(value, dict) else {}
        data[platform] = text
        await _set_json(_K_COOKIES, data)
    except Exception as exc:
        log.warning("upstash cookie save failed: %s", exc)


# --- bot settings ----------------------------------------------------------

async def load_settings():
    """Bot settings dict (upload destinations, ...) or None."""
    if not _enabled:
        return None
    value = await _get_json(_K_SETTINGS)
    if value is None:
        return None
    if value is _MISSING:
        return {}
    return value if isinstance(value, dict) else {}


async def save_setting(key: str, value) -> None:
    if not _enabled:
        return
    try:
        current = await _get_json(_K_SETTINGS)
        if current is None:
            return
        data = current if isinstance(current, dict) else {}
        data[key] = value
        await _set_json(_K_SETTINGS, data)
    except Exception as exc:
        log.warning("upstash setting save failed: %s", exc)


async def close() -> None:
    """Mark the backend down (REST calls are per-request; there is no
    connection to close)."""
    global _enabled
    _enabled = False
