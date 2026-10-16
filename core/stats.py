"""Persistent usage stats: bandwidth + job counters.

Kept in a small JSON file (gitignored) so numbers survive restarts.
Every record updates both the all-time total and the current calendar
month — the monthly view is what matters against Koyeb's 100 GB/month
free outbound allowance (uploads to Telegram + file-to-link serves).

Kinds:
  downloaded — bytes pulled from the internet (all engines + restricted)
  uploaded   — bytes sent to Telegram (every send_files call)
  served     — bytes streamed out by the file-to-link web server
"""

import json
import os
import tempfile
import time

import config

DATA_FILE = os.path.join(config.BASE_DIR, "data", "stats.json")

_started_at = time.time()


def _blank_period() -> dict:
    return {"downloaded": 0, "uploaded": 0, "served": 0, "jobs": {}}


def _blank() -> dict:
    return {"total": _blank_period(), "months": {}}


def _month_key() -> str:
    return time.strftime("%Y-%m")


def _load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and "total" in data:
            return data
    except Exception:
        pass
    return _blank()


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DATA_FILE),
                                   prefix=".stats", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass  # stats must never break a transfer


def _period(data: dict, key: str) -> dict:
    if key == "total":
        return data["total"]
    return data["months"].setdefault(key, _blank_period())


def record(kind: str, nbytes: int) -> None:
    """Add `nbytes` to `kind` (downloaded|uploaded|served), total + month."""
    nbytes = int(nbytes or 0)
    if nbytes <= 0 or kind not in ("downloaded", "uploaded", "served"):
        return
    data = _load()
    month = _month_key()
    for key in ("total", month):
        _period(data, key)[kind] += nbytes
    _save(data)


def record_paths(kind: str, paths) -> None:
    """Record the on-disk size of `paths` under `kind` (ignores missing)."""
    total = 0
    for path in paths or []:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    record(kind, total)


def record_job(feature: str, ok: bool = True) -> None:
    """Count one finished job for `feature` (uploader, restricted,
    file-to-link, mediainfo)."""
    data = _load()
    month = _month_key()
    for key in ("total", month):
        jobs = _period(data, key)["jobs"]
        entry = jobs.setdefault(feature, {"ok": 0, "failed": 0})
        entry["ok" if ok else "failed"] += 1
    _save(data)


def snapshot() -> dict:
    """Uptime + total/monthly numbers, ready to render."""
    data = _load()
    month = _month_key()
    return {
        "uptime": time.time() - _started_at,
        "month_key": month,
        "total": data["total"],
        "month": data["months"].get(month, _blank_period()),
    }
