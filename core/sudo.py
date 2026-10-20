"""Sudo users: extra Telegram ids allowed to use the bot like the owner.

Sources, in the order is_admin() checks them:
  * the SUDO_USERS env var (config.py) — permanent, survives everything.
  * runtime ids added via /addsudo (owner only), stored in:
      - MongoDB when DB_URI is set (core/db.py) — survives redeploys;
      - data/sudo.json otherwise — on hosts with ephemeral disks
        (Render/Koyeb free restarts) the file alone is not durable.

The runtime set is cached in memory (loaded once), so permission
checks stay instant; the JSON file is still refreshed on every change
as a local fallback copy (and the backup vault keeps a copy off-host).

Only the OWNER can add/remove sudo users. Sudo users get every bot
feature; strangers (not owner, not sudo) match no handler and get no
reply at all.
"""

import json
import os
import tempfile

import config
from core import db

DATA_FILE = os.path.join(config.BASE_DIR, "data", "sudo.json")

_cache = None  # set[int] of runtime ids once loaded


def _load_file() -> set:
    try:
        with open(DATA_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return {int(x) for x in data
                    if str(x).lstrip("-").isdigit()}
    except Exception:
        pass
    return set()


def _save_file(ids: set) -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DATA_FILE),
                                   prefix=".sudo", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(sorted(ids), handle)
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass  # sudo bookkeeping must never break a transfer


def _runtime_ids() -> set:
    global _cache
    if _cache is None:
        _cache = _load_file()
    return _cache


async def init_from_db() -> None:
    """At startup: Mongo (when enabled) is the runtime-sudo truth.

    Mongo ids win; when Mongo is empty but the local file has ids,
    they are migrated up so nothing is lost in the switch.
    """
    global _cache
    mongo_ids = await db.load_sudo_ids()
    if mongo_ids is None:
        return  # file mode
    if mongo_ids:
        _cache = set(mongo_ids)
        _save_file(_cache)  # refresh the local fallback copy
    else:
        local = _runtime_ids()
        if local:
            await db.save_sudo_ids(local)


def is_owner(user_id) -> bool:
    return bool(user_id and config.OWNER_ID
                and user_id == config.OWNER_ID)


def is_admin(user_id) -> bool:
    """Owner or sudo user — allowed to use every bot feature."""
    if not user_id:
        return False
    return (is_owner(user_id)
            or user_id in config.SUDO_USERS
            or user_id in _runtime_ids())


def add_sudo(user_id: int) -> bool:
    """Add a runtime sudo user. True if newly added."""
    user_id = int(user_id)
    if is_admin(user_id):
        return False
    ids = _runtime_ids()
    ids.add(user_id)
    _save_file(ids)
    db.schedule(db.save_sudo_ids(set(ids)))
    return True


def remove_sudo(user_id: int) -> bool:
    """Remove a runtime sudo user. True if one was removed."""
    ids = _runtime_ids()
    if int(user_id) not in ids:
        return False
    ids.discard(int(user_id))
    _save_file(ids)
    db.schedule(db.save_sudo_ids(set(ids)))
    return True


def list_sudo() -> tuple:
    """(env_var_ids, runtime_ids) — both as sets of ints."""
    return set(config.SUDO_USERS), set(_runtime_ids())
