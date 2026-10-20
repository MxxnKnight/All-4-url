"""MongoDB storage (optional, recommended).

Enabled when DB_URI is set. DB_NAME chooses the database on that
cluster — the same URI is shared with the owner's other bots, so this
bot keeps its own database (DB_NAME) and, inside it, its own
collections:

    sudo   {_id: "runtime", ids: [123, 456]}           — /addsudo users
    usage  {_id: "total" | "2026-10" | "2026-10-21",   — bandwidth + jobs
            downloaded, uploaded, served,
            jobs: {feature: {ok, failed}}}

Cookies are deliberately NOT stored here: a shared database is the
wrong place for login credentials — those live in the backup vault
(core/backup.py) instead.

If DB_URI is unset or Mongo is unreachable, the bot falls back to the
local JSON files exactly as before — Mongo is an upgrade, never a
single point of failure.
"""

import asyncio
import logging

import config

log = logging.getLogger("all4url")

_client = None
_db = None


def enabled() -> bool:
    return _db is not None


async def init() -> bool:
    """Connect + ping. Returns True when Mongo is usable."""
    global _client, _db
    if not config.DB_URI:
        log.info("DB_URI not set — using local JSON storage")
        return False
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        log.warning("motor not installed — using local JSON storage")
        return False
    try:
        _client = AsyncIOMotorClient(config.DB_URI,
                                     serverSelectionTimeoutMS=5000)
        await _client.admin.command("ping")
        _db = _client[config.DB_NAME]
        log.info("MongoDB connected (database: %s)", config.DB_NAME)
        return True
    except Exception as exc:
        log.warning("MongoDB unavailable (%s) — using local JSON storage",
                    exc)
        _client, _db = None, None
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
    """Runtime sudo ids from Mongo, or None when unavailable."""
    if _db is None:
        return None
    try:
        doc = await _db["sudo"].find_one({"_id": "runtime"})
        if doc and isinstance(doc.get("ids"), list):
            return {int(x) for x in doc["ids"]}
        return set()
    except Exception as exc:
        log.warning("mongo sudo load failed: %s", exc)
    return None


async def save_sudo_ids(ids) -> None:
    if _db is None:
        return
    try:
        await _db["sudo"].replace_one(
            {"_id": "runtime"},
            {"_id": "runtime", "ids": sorted(int(x) for x in ids)},
            upsert=True)
    except Exception as exc:
        log.warning("mongo sudo save failed: %s", exc)


# --- usage ---------------------------------------------------------------

async def load_usage():
    """All usage docs as {bucket_id: doc}, or None when unavailable."""
    if _db is None:
        return None
    try:
        out = {}
        async for doc in _db["usage"].find({}):
            out[doc["_id"]] = doc
        return out
    except Exception as exc:
        log.warning("mongo usage load failed: %s", exc)
    return None


async def inc_usage(buckets: dict) -> bool:
    """$inc several usage buckets. buckets maps bucket id -> flat field
    paths ({"downloaded": n, "jobs.uploader.ok": n}). True if written."""
    if _db is None or not buckets:
        return False
    try:
        for bucket, fields in buckets.items():
            if fields:
                await _db["usage"].update_one(
                    {"_id": bucket}, {"$inc": fields}, upsert=True)
        return True
    except Exception as exc:
        log.warning("mongo usage write failed: %s", exc)
        return False


async def close() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client, _db = None, None
