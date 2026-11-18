"""Per-user bot settings (v1.45.0).

Each admin — the owner and every sudo user — has their OWN copy of
every setting, changed from /destination. A user who never changed
one falls back to the shared (global) value, which is what
/destination used to set for everyone: v1.44.x and earlier stored
only the global value, and those keep working as the fallback, so
nothing changes for anyone until they open /destination themselves.

    dest_uploader    "bot" | "saved"
    dest_restricted  "bot" | "saved"
    sendas_uploader  "video" | "document"

"bot"   = the bot sends files to the chat (default, classic behavior).
"saved" = the user account uploads them to its own Saved Messages
          (Destiny-style; needs SESSION_STRING — sender falls back to
          "bot" when no session is configured).

"video"    = video files go out as videos (inline player, thumbnail).
"document" = video files go out as plain files instead. Photos, audio
             and everything else are unaffected, and restricted
             downloads always use "video" — the setting is URL uploads
             only, by the owner's choice.

Storage mirrors sudo/stats: Mongo `settings` collection when DB_URI
is set, data/settings.json otherwise; values cached in memory. Both
stores hold one flat dict — global keys as before ("dest_uploader")
and per-user keys suffixed with the Telegram id
("dest_uploader:123456") — so no migration is needed.
"""

import json
import os
import re
import tempfile

import config
from core import db

DATA_FILE = os.path.join(config.BASE_DIR, "data", "settings.json")

_DEFAULTS = {"dest_uploader": "bot", "dest_restricted": "bot",
             "sendas_uploader": "video"}
_DESTS = ("bot", "saved")
_SENDAS = ("video", "document")
_VALID = {"dest_uploader": _DESTS, "dest_restricted": _DESTS,
          "sendas_uploader": _SENDAS}
_USER_KEY = re.compile(r"^(dest_uploader|dest_restricted|sendas_uploader)"
                       r":(\d+)$")

_mem = None


def _base_and_user(key: str):
    """Split a stored key into (base, user_id|None); None when the
    key is not a setting at all."""
    if key in _DEFAULTS:
        return key, None
    match = _USER_KEY.match(key)
    if match:
        return match.group(1), int(match.group(2))
    return None, None


def _apply(data: dict, raw: dict) -> None:
    """Copy every valid setting from a stored dict into `data`
    (global keys and per-user keys alike)."""
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        base, _user_id = _base_and_user(key)
        if base is not None and value in _VALID[base]:
            data[key] = value


def _load_file() -> dict:
    data = dict(_DEFAULTS)
    try:
        with open(DATA_FILE, encoding="utf-8") as handle:
            _apply(data, json.load(handle))
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
        _apply(data, stored)
        _mem = data
        _save_file(data)
    else:
        for key, value in _memory().items():
            await db.save_setting(key, value)


def _get(base: str, default: str, user_id=None) -> str:
    """A user's own value when set, else the shared global value,
    else the default."""
    data = _memory()
    if user_id:
        own = data.get(f"{base}:{int(user_id)}")
        if own in _VALID[base]:
            return own
    value = data.get(base, default)
    return value if value in _VALID[base] else default


def _set(base: str, value: str, user_id=None) -> None:
    if value not in _VALID[base]:
        return
    key = f"{base}:{int(user_id)}" if user_id else base
    data = _memory()
    data[key] = value
    _save_file(data)
    db.schedule(db.save_setting(key, value))


def get_dest(feature: str, user_id=None) -> str:
    """Upload destination for "uploader" | "restricted", for one
    user (their own setting, else the shared one).

    "saved" is owner-only by construction: the Saved Messages
    vault belongs to the single user session (the owner's
    account), so a sudo user who inherited "saved" would watch
    their files vanish into the owner's vault and get nothing
    back. Any non-owner requester is downgraded to "bot"."""
    value = _get(f"dest_{feature}", "bot", user_id)
    if (value == "saved" and user_id
            and int(user_id) != config.OWNER_ID):
        return "bot"
    return value


def set_dest(feature: str, value: str, user_id=None) -> None:
    """Set the destination — for one user when user_id is given
    (their /destination), else the shared value. A non-owner
    cannot pick "saved" (see get_dest); the request is ignored."""
    if (value == "saved" and user_id
            and int(user_id) != config.OWNER_ID):
        return
    _set(f"dest_{feature}", value, user_id)


def get_send_as(feature: str, user_id=None) -> str:
    """How video files are sent for `feature`: "video" (inline player)
    or "document" (plain file). Only "uploader" has the setting —
    every other feature always sends videos as video."""
    return _get(f"sendas_{feature}", "video", user_id)


def set_send_as(feature: str, value: str, user_id=None) -> None:
    _set(f"sendas_{feature}", value, user_id)
