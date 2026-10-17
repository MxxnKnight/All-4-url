"""Sudo users: extra Telegram ids allowed to use the bot like the owner.

Two sources:
  * the SUDO_USERS env var (config.py) — permanent, survives restarts
    and redeploys. This is the durable home for sudo users.
  * data/sudo.json — ids added at runtime via /addsudo (owner only).
    On hosts with ephemeral disks (Render free) this file is lost on
    every redeploy, so it is only good for temporary sudo users.

Only the OWNER can add/remove sudo users. Sudo users get every bot
feature; strangers (not owner, not sudo) match no handler and get no
reply at all.
"""

import json
import os
import tempfile

import config

DATA_FILE = os.path.join(config.BASE_DIR, "data", "sudo.json")


def _load_runtime() -> set:
    try:
        with open(DATA_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return {int(x) for x in data
                    if str(x).lstrip("-").isdigit()}
    except Exception:
        pass
    return set()


def _save_runtime(ids: set) -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DATA_FILE),
                                   prefix=".sudo", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(sorted(ids), handle)
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass  # sudo bookkeeping must never break a transfer


def is_owner(user_id) -> bool:
    return bool(user_id and config.OWNER_ID
                and user_id == config.OWNER_ID)


def is_admin(user_id) -> bool:
    """Owner or sudo user — allowed to use every bot feature."""
    if not user_id:
        return False
    return (is_owner(user_id)
            or user_id in config.SUDO_USERS
            or user_id in _load_runtime())


def add_sudo(user_id: int) -> bool:
    """Add a runtime sudo user. True if newly added."""
    user_id = int(user_id)
    if is_admin(user_id):
        return False
    ids = _load_runtime()
    ids.add(user_id)
    _save_runtime(ids)
    return True


def remove_sudo(user_id: int) -> bool:
    """Remove a runtime sudo user. True if one was removed."""
    ids = _load_runtime()
    if int(user_id) not in ids:
        return False
    ids.discard(int(user_id))
    _save_runtime(ids)
    return True


def list_sudo() -> tuple:
    """(env_var_ids, runtime_ids) — both as sets of ints."""
    return set(config.SUDO_USERS), _load_runtime()
