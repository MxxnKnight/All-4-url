"""Usage stats: bandwidth + job counters — daily, monthly, all-time.

Storage:
  * MongoDB when DB_URI is set (core/db.py, `usage` collection) —
    survives redeploys on any host. Counters live in memory; deltas
    flush to Mongo every 30 s and on shutdown.
  * data/stats.json otherwise (gitignored). The file is also refreshed
    on every Mongo flush, as a local fallback copy.

Kinds:
  downloaded — bytes pulled from the internet (all engines + restricted)
  uploaded   — bytes sent to Telegram (every send_files call)
  served     — bytes streamed out by the file-to-link web server
"""

import asyncio
import json
import os
import tempfile
import time

import config
from core import db

DATA_FILE = os.path.join(config.BASE_DIR, "data", "stats.json")

_started_at = time.time()
_KEEP_DAYS = 62  # daily buckets kept in memory/file (Mongo keeps all)


def _blank_period() -> dict:
    return {"downloaded": 0, "uploaded": 0, "served": 0, "jobs": {}}


def _blank_all() -> dict:
    return {"total": _blank_period(), "months": {}, "days": {}}


def _month_key() -> str:
    return time.strftime("%Y-%m")


def _day_key() -> str:
    return time.strftime("%Y-%m-%d")


def _clean_period(raw) -> dict:
    """Coerce a stored period (file or Mongo doc) into the known shape."""
    period = _blank_period()
    if isinstance(raw, dict):
        for kind in ("downloaded", "uploaded", "served"):
            try:
                period[kind] = int(raw.get(kind) or 0)
            except (TypeError, ValueError):
                pass
        jobs = raw.get("jobs")
        if isinstance(jobs, dict):
            for feature, entry in jobs.items():
                if isinstance(entry, dict):
                    period["jobs"][str(feature)] = {
                        "ok": int(entry.get("ok") or 0),
                        "failed": int(entry.get("failed") or 0)}
    return period


def _load_file() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and "total" in data:
            out = _blank_all()
            out["total"] = _clean_period(data["total"])
            for key, val in (data.get("months") or {}).items():
                out["months"][key] = _clean_period(val)
            for key, val in (data.get("days") or {}).items():
                out["days"][key] = _clean_period(val)
            return out
    except Exception:
        pass
    return _blank_all()


def _prune_days(data: dict) -> None:
    days = sorted(data["days"])
    for key in days[:-_KEEP_DAYS]:
        del data["days"][key]


def _save_file(data: dict) -> None:
    try:
        _prune_days(data)
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DATA_FILE),
                                   prefix=".stats", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass  # stats must never break a transfer


_mem = None        # the live counters, loaded once
_pending = {}      # bucket -> {field_path: delta} awaiting Mongo flush


def _memory() -> dict:
    global _mem
    if _mem is None:
        _mem = _load_file()
    return _mem


def _period(data: dict, bucket: str) -> dict:
    if bucket == "total":
        return data["total"]
    if len(bucket) == 7:
        return data["months"].setdefault(bucket, _blank_period())
    return data["days"].setdefault(bucket, _blank_period())


def _bump(bucket: str, field_path: str, delta: int) -> None:
    fields = _pending.setdefault(bucket, {})
    fields[field_path] = fields.get(field_path, 0) + delta


def _buckets():
    return ("total", _month_key(), _day_key())


# --- bandwidth guard -----------------------------------------------------
# Outbound = this month's uploaded + served (what the host meters).
# record() fires the alert callback when a threshold is *crossed*
# (state-based, so a restart after crossing never re-fires, and the
# monthly reset naturally re-arms it).

_alert_cb = None


def set_alert_callback(cb) -> None:
    """cb(level, used_bytes, cap_bytes) — an async function."""
    global _alert_cb
    _alert_cb = cb


def outbound_month() -> int:
    month = _memory()["months"].get(_month_key(), _blank_period())
    return month["uploaded"] + month["served"]


def outbound_pct() -> float:
    cap = config.OUTBOUND_CAP_BYTES
    return (outbound_month() / cap * 100) if cap else 0.0


def bandwidth_blocked() -> bool:
    """True when new download jobs should pause (STOP threshold hit)."""
    return outbound_pct() >= config.BANDWIDTH_STOP_PCT


def bandwidth_pause_text() -> str:
    return (f"⛔ **Bandwidth paused** — this month's outbound is at "
            f"{outbound_pct():.0f}% of the {config.OUTBOUND_CAP_GB} GB "
            "monthly cap. New downloads pause until the 1st; "
            "streaming links still work.")


def _check_alerts(prev_out: int, new_out: int) -> None:
    if _alert_cb is None:
        return
    cap = config.OUTBOUND_CAP_BYTES
    if not cap:
        return
    for level, thr in (("warn", config.BANDWIDTH_WARN_PCT),
                       ("stop", config.BANDWIDTH_STOP_PCT)):
        if prev_out * 100 < cap * thr <= new_out * 100:
            db.schedule(_alert_cb(level, new_out, cap))


def record(kind: str, nbytes: int) -> None:
    """Add `nbytes` to `kind` (downloaded|uploaded|served)."""
    nbytes = int(nbytes or 0)
    if nbytes <= 0 or kind not in ("downloaded", "uploaded", "served"):
        return
    data = _memory()
    prev_out = None
    if kind in ("uploaded", "served"):
        mp = _period(data, _month_key())
        prev_out = mp["uploaded"] + mp["served"]
    for bucket in _buckets():
        _period(data, bucket)[kind] += nbytes
        if db.enabled():
            _bump(bucket, kind, nbytes)
    if not db.enabled():
        _save_file(data)
    if prev_out is not None:
        mp = _period(data, _month_key())
        _check_alerts(prev_out, mp["uploaded"] + mp["served"])


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
    data = _memory()
    outcome = "ok" if ok else "failed"
    for bucket in _buckets():
        jobs = _period(data, bucket)["jobs"]
        entry = jobs.setdefault(feature, {"ok": 0, "failed": 0})
        entry[outcome] += 1
        if db.enabled():
            _bump(bucket, f"jobs.{feature}.{outcome}", 1)
    if not db.enabled():
        _save_file(data)


def _deltas_of(data: dict) -> dict:
    """Whole-state -> per-bucket $inc deltas (used for migration)."""
    out = {}
    all_buckets = (("total", data["total"]),
                   *data["months"].items(), *data["days"].items())
    for bucket, period in all_buckets:
        fields = {}
        for kind in ("downloaded", "uploaded", "served"):
            if period[kind]:
                fields[kind] = period[kind]
        for feature, entry in period["jobs"].items():
            if entry["ok"]:
                fields[f"jobs.{feature}.ok"] = entry["ok"]
            if entry["failed"]:
                fields[f"jobs.{feature}.failed"] = entry["failed"]
        if fields:
            out[bucket] = fields
    return out


async def init_from_db() -> None:
    """At startup: Mongo (when enabled) is the usage truth.

    Mongo docs win; when Mongo is empty but the local file has data,
    the file state is migrated up so nothing is lost in the switch.
    """
    global _mem
    docs = await db.load_usage()
    if docs is None:
        return  # file mode — memory already loads the file lazily
    if docs:
        data = _blank_all()
        for bucket, doc in docs.items():
            period = _clean_period(doc)
            if bucket == "total":
                data["total"] = period
            elif len(bucket) == 7:
                data["months"][bucket] = period
            elif len(bucket) == 10:
                data["days"][bucket] = period
        _mem = data
        _prune_days(_mem)
        _save_file(_mem)  # refresh the local fallback copy
    else:
        data = _memory()
        deltas = _deltas_of(data)
        if deltas:
            await db.inc_usage(deltas)


async def flush() -> None:
    """Push pending deltas to Mongo; refresh the local file copy."""
    if not db.enabled():
        return
    if _pending:
        pending = {bucket: dict(fields)
                   for bucket, fields in _pending.items()}
        if await db.inc_usage(pending):
            for bucket, fields in pending.items():
                live = _pending.get(bucket, {})
                for field, n in fields.items():
                    left = live.get(field, 0) - n
                    if left > 0:
                        live[field] = left
                    else:
                        live.pop(field, None)
                if not live:
                    _pending.pop(bucket, None)
    _save_file(_memory())


async def flusher(interval: int = 30) -> None:
    """Background loop: flush() every `interval` seconds."""
    while True:
        await asyncio.sleep(interval)
        try:
            await flush()
        except Exception:
            pass  # flush() already guards; stats must never break


def snapshot() -> dict:
    """Uptime + today/month/total numbers, ready to render."""
    data = _memory()
    month, day = _month_key(), _day_key()
    return {
        "uptime": time.time() - _started_at,
        "month_key": month,
        "day_key": day,
        "total": data["total"],
        "month": data["months"].get(month, _blank_period()),
        "day": data["days"].get(day, _blank_period()),
    }
