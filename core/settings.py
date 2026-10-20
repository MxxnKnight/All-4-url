"""Bot-wide settings the owner can change from chat.

Currently: the upload destination per feature — where finished files
are delivered.

    dest_uploader    "bot" | "saved"
    dest_restricted  "bot" | "saved"

"bot"   = the bot sends files to the chat (default, classic behavior).
"saved" = the user account uploads them to its own Saved Messages
          (Destiny-style; needs SESSION_STRING — sender falls back to
          "bot" when no session is configured).

Storage mirrors sudo/stats: Mongo `settings` collection when DB_URI
is set, data/settings.json otherwise; values cached in memory.
"""

import json
import os
import tempfile

import config
from core import db

DATA_FILE = os.path.join(config.BASE_DIR, "data", "settings.json")

_DEFAULTS = {"dest_uploader": "bot", "dest_restricted": "bot"}
_DESTS = ("bot", "saved")

_mem = None


def _load_file() -> dict:
    data = dict(_DEFAULTS)
    try:
        with open(DATA_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            for key in _DEFAULTS:
                if raw.get(key) in _DESTS:
                    data[key] = raw[key]
    except Exception:
        pass
    return data


def _save_file(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DATA_FILE),
                                   prefix=".settings", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass  # settings must never break a transfer


def _memory() -> dict:
    global _mem
    if _mem is None:
        _mem = _load_file()
    return _mem


async def init_from_db() -> None:
    """At startup: Mongo (when enabled) is the settings truth; local
    values migrate up on first run."""
    global _mem
    stored = await db.load_settings()
    if stored is None:
        return  # file mode
    if stored:
        data = dict(_DEFAULTS)
        for key in _DEFAULTS:
            if stored.get(key) in _DESTS:
                data[key] = stored[key]
        _mem = data
        _save_file(data)
    else:
        for key, value in _memory().items():
            await db.save_setting(key, value)


def get_dest(feature: str) -> str:
    """Upload destination for "uploader" | "restricted"."""
    value = _memory().get(f"dest_{feature}", "bot")
    return value if value in _DESTS else "bot"


def set_dest(feature: str, value: str) -> None:
    if value not in _DESTS:
        return
    data = _memory()
    data[f"dest_{feature}"] = value
    _save_file(data)
    db.schedule(db.save_setting(f"dest_{feature}", value))
