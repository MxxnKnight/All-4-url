"""Telegram Mini App — the bot's control panel as a web page.

Served by the existing aiohttp server at GET /app (the Koyeb URL).
The page is styled with Telegram's own theme variables, so it looks
like a normal Mini App, and talks to three JSON endpoints:

    POST /api/state        usage, bandwidth, jars, destinations
    POST /api/cookies      paste-a-jar install (the phone-friendly
                           replacement for juggling cookie files)
    POST /api/destination  change upload destinations

Every API call carries Telegram's initData, re-verified server-side
on each request (HMAC-SHA256 with the bot token, per Telegram's
WebApp spec) — and the signed user must be the OWNER. The panel can
install cookies and change settings, so nobody else gets in.
"""

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config
from core import cookies, db, settings, stats, sudo
from core.utils import human_bytes

log = logging.getLogger("all4url")

#: initData older than this is refused (replay guard).
MAX_INITDATA_AGE = 24 * 3600
#: Pasted cookie text cap (real jars are far smaller).
MAX_PASTE_CHARS = 1_000_000


def validate_init_data(init_data: str, token: str):
    """Verify Telegram WebApp initData → {"user_id", "first_name"}.

    Returns None for anything unsigned, tampered, stale or malformed.
    Implements Telegram's documented check: sort the fields, join as
    key=value lines, HMAC with key derived from the bot token.
    """
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    given = pairs.pop("hash", None)
    if not given:
        return None
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(),
                      hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, given):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, TypeError, KeyError):
        return None
    if time.time() - auth_date > MAX_INITDATA_AGE:
        return None
    return {"user_id": user_id,
            "first_name": str(user.get("first_name", ""))}


async def _owner(request: web.Request):
    """(fields, body) for an owner-signed call, else (None, error)."""
    try:
        body = await request.json()
    except Exception:
        return None, None, web.json_response(
            {"ok": False, "error": "bad request"}, status=400)
    fields = validate_init_data(str(body.get("initData", "")),
                                config.BOT_TOKEN)
    if fields is None:
        return None, None, web.json_response(
            {"ok": False, "error": "Telegram signature check failed — "
                                   "open the panel from the bot."},
            status=401)
    if fields["user_id"] != config.OWNER_ID:
        return None, None, web.json_response(
            {"ok": False, "error": "Owner only."}, status=403)
    return fields, body, None


def _period(bucket: dict) -> dict:
    return {"downloaded": human_bytes(bucket.get("downloaded", 0)),
            "uploaded": human_bytes(bucket.get("uploaded", 0)),
            "served": human_bytes(bucket.get("served", 0)),
            "jobs": int(bucket.get("total_jobs", 0)),
            "failed": int(bucket.get("failed_jobs", 0))}


def _state_payload() -> dict:
    snap = stats.snapshot()
    installed = {pid: count for pid, _l, count, _t in cookies.list_jars()}
    jars = [{"id": pid, "label": label,
             "installed": pid in installed,
             "count": installed.get(pid, 0)}
            for pid, label, _d in cookies.COOKIE_SITES]
    used = (snap["month"].get("uploaded", 0)
            + snap["month"].get("served", 0))
    return {
        "ok": True,
        "version": config.BOT_VERSION,
        "storage": (f"MongoDB ({config.DB_NAME})" if db.enabled()
                    else "local files — no database"),
        "uptime": int(snap["uptime"]),
        "session": bool(config.SESSION_STRING),
        "sudo_count": len(sudo.list_sudo()),
        "bandwidth": {
            "used": human_bytes(used),
            "cap_gb": config.OUTBOUND_CAP_GB,
            "pct": round(stats.outbound_pct(), 1),
            "state": ("stop" if stats.bandwidth_blocked()
                      else "warn" if stats.outbound_pct()
                      >= config.BANDWIDTH_WARN_PCT
                      else "ok")},
        "usage": {"today": _period(snap["day"]),
                  "month": _period(snap["month"]),
                  "total": _period(snap["total"])},
        "jars": jars,
        "dest": {"uploader": settings.get_dest("uploader"),
                 "restricted": settings.get_dest("restricted")},
    }


async def api_state(request: web.Request) -> web.Response:
    _fields, _body, error = await _owner(request)
    if error is not None:
        return error
    return web.json_response(_state_payload())


async def api_cookies(request: web.Request) -> web.Response:
    _fields, body, error = await _owner(request)
    if error is not None:
        return error
    platform = str(body.get("platform", "")).strip().lower()
    text = str(body.get("text", ""))
    sites = {pid: (label, domains)
             for pid, label, domains in cookies.COOKIE_SITES}
    if platform not in sites:
        return web.json_response(
            {"ok": False, "error": "Pick a site first."}, status=400)
    if len(text) > MAX_PASTE_CHARS:
        return web.json_response(
            {"ok": False, "error": "That paste is too large for a "
                                   "cookie file."}, status=400)
    label, domains = sites[platform]
    try:
        count, found = cookies.parse_cookies(text)
    except Exception:
        count, found = 0, []
    if count == 0:
        return web.json_response(
            {"ok": False, "error": "No cookie lines found — paste the "
                                   "full cookies.txt content."},
            status=400)
    if not any(cookies.domain_matches(d, domains) for d in found):
        return web.json_response(
            {"ok": False,
             "error": f"No {label} cookies in that text — wrong site's "
                      "file?"}, status=400)
    try:
        count, _domains = cookies.install_jar(platform, text)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)},
                                 status=400)
    logged_in = cookies.looks_logged_in(platform, text)
    log.info("cookies for %s installed via mini app (%d cookies)",
             platform, count)
    return web.json_response(
        {"ok": True, "count": count, "logged_in": logged_in,
         "warning": None if logged_in else
                    f"Installed, but no {label} login cookie was found "
                    "— the site may still block the bot. Export while "
                    "logged in."})


async def api_destination(request: web.Request) -> web.Response:
    _fields, body, error = await _owner(request)
    if error is not None:
        return error
    feature = str(body.get("feature", ""))
    dest = str(body.get("dest", ""))
    if feature not in ("uploader", "restricted") \
            or dest not in ("bot", "saved"):
        return web.json_response({"ok": False, "error": "bad values"},
                                 status=400)
    if dest == "saved" and not config.SESSION_STRING:
        return web.json_response(
            {"ok": False, "error": "Saved Messages needs SESSION_STRING "
                                   "set on the host first."},
            status=400)
    settings.set_dest(feature, dest)
    log.info("destination for %s set to %s via mini app", feature, dest)
    return web.json_response({"ok": True,
                              "dest": settings.get_dest(feature)})


async def page(_request: web.Request) -> web.Response:
    return web.Response(text=PAGE_HTML, content_type="text/html")


def add_routes(app: web.Application) -> None:
    app.router.add_get("/app", page)
    app.router.add_post("/api/state", api_state)
    app.router.add_post("/api/cookies", api_cookies)
    app.router.add_post("/api/destination", api_destination)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>All-4-url Panel</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg: var(--tg-theme-bg-color, #ffffff);
  --text: var(--tg-theme-text-color, #000000);
  --hint: var(--tg-theme-hint-color, #8a8a8e);
  --btn: var(--tg-theme-button-color, #2481cc);
  --btn-text: var(--tg-theme-button-text-color, #ffffff);
  --secondary: var(--tg-theme-secondary-bg-color, #f2f2f7);
  --link: var(--tg-theme-link-color, #2481cc);
}
*{box-sizing:border-box}
body{margin:0;padding:14px;font-family:-apple-system,system-ui,Roboto,sans-serif;
  background:var(--bg);color:var(--text)}
h1{font-size:19px;margin:2px 0 2px}
.sub{color:var(--hint);font-size:13px;margin-bottom:12px}
.card{background:var(--secondary);border-radius:14px;padding:14px;margin-bottom:12px}
.card h2{font-size:15px;margin:0 0 10px}
.row{display:flex;justify-content:space-between;font-size:14px;padding:3px 0}
.row span:first-child{color:var(--hint)}
.bar{height:8px;border-radius:4px;background:rgba(127,127,127,.25);overflow:hidden;margin:8px 0 4px}
.bar>i{display:block;height:100%;background:var(--btn);border-radius:4px}
.bar>i.warn{background:#f5a623}.bar>i.stop{background:#e5484d}
select,textarea{width:100%;border:1px solid rgba(127,127,127,.35);border-radius:10px;
  background:var(--bg);color:var(--text);padding:10px;font-size:14px}
textarea{min-height:110px;font-family:ui-monospace,monospace;font-size:12px;margin-top:8px}
button.main{width:100%;margin-top:10px;padding:12px;border:0;border-radius:10px;
  background:var(--btn);color:var(--btn-text);font-size:15px;font-weight:600}
.msg{font-size:13px;margin-top:8px;min-height:16px}
.msg.err{color:#e5484d}.msg.ok{color:#30a46c}
.seg{display:flex;background:var(--bg);border-radius:10px;padding:3px;gap:3px}
.seg button{flex:1;border:0;border-radius:8px;padding:8px;font-size:13.5px;
  background:transparent;color:var(--text)}
.seg button.on{background:var(--btn);color:var(--btn-text);font-weight:600}
.seglabel{font-size:13px;color:var(--hint);margin:10px 0 6px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;
  background:#c0c0c6}.dot.y{background:#30a46c}
.jar{display:flex;justify-content:space-between;font-size:14px;padding:4px 0}
.jar span:last-child{color:var(--hint);font-size:13px}
.foot{color:var(--hint);font-size:12px;text-align:center;margin:14px 0 4px}
</style>
</head>
<body>
<h1>🎛 All-4-url Panel</h1>
<div class="sub" id="sub">Loading…</div>

<div class="card">
  <h2>📊 Usage</h2>
  <div id="usage"></div>
  <div class="seglabel">Monthly outbound bandwidth</div>
  <div class="bar"><i id="bwbar" style="width:0%"></i></div>
  <div class="row"><span id="bwtext">—</span><span id="bwstate"></span></div>
</div>

<div class="card">
  <h2>🍪 Cookie jars</h2>
  <div id="jars"></div>
  <div class="seglabel">Paste cookies.txt for:</div>
  <select id="site"></select>
  <textarea id="paste" placeholder="Paste the full cookies.txt content here…"></textarea>
  <button class="main" id="install">Install cookies</button>
  <div class="msg" id="cookiemsg"></div>
</div>

<div class="card">
  <h2>🎯 Upload destination</h2>
  <div class="seglabel">🔗 URL uploads</div>
  <div class="seg" id="dest-uploader">
    <button data-d="bot">💬 Bot PM</button><button data-d="saved">💾 Saved Messages</button>
  </div>
  <div class="seglabel">📥 Restricted downloads</div>
  <div class="seg" id="dest-restricted">
    <button data-d="bot">💬 Bot PM</button><button data-d="saved">💾 Saved Messages</button>
  </div>
  <div class="msg" id="destmsg"></div>
</div>

<div class="foot" id="foot"></div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg ? tg.initData : "";
const $ = id => document.getElementById(id);

async function api(path, extra) {
  const r = await fetch(path, {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(Object.assign({initData}, extra || {}))});
  return r.json();
}
function period(name, p) {
  return `<div class="row"><span>${name}</span><span>⬇ ${p.downloaded} · ⬆ ${p.uploaded}</span></div>
          <div class="row"><span></span><span style="color:var(--hint)">${p.jobs} jobs${p.failed ? " · " + p.failed + " failed" : ""}</span></div>`;
}
function paintSeg(id, val) {
  document.querySelectorAll("#" + id + " button").forEach(b =>
    b.classList.toggle("on", b.dataset.d === val));
}
async function load() {
  const s = await api("/api/state");
  if (!s.ok) { $("sub").textContent = s.error || "Could not load."; return; }
  const up = Math.floor(s.uptime / 3600) + "h " + Math.floor(s.uptime % 3600 / 60) + "m";
  $("sub").textContent = `v${s.version} · 🗄 ${s.storage} · ⏱ ${up} uptime`;
  $("usage").innerHTML = period("Today", s.usage.today)
                       + period("This month", s.usage.month)
                       + period("All time", s.usage.total);
  $("bwbar").style.width = Math.min(100, s.bandwidth.pct) + "%";
  $("bwbar").className = s.bandwidth.state === "ok" ? "" : s.bandwidth.state;
  $("bwtext").textContent = `${s.bandwidth.used} of ${s.bandwidth.cap_gb} GB (${s.bandwidth.pct}%)`;
  $("bwstate").textContent = s.bandwidth.state === "ok" ? "🟢 fine"
    : s.bandwidth.state === "warn" ? "🟠 near cap" : "🔴 downloads paused";
  $("jars").innerHTML = s.jars.map(j =>
    `<div class="jar"><span><i class="dot ${j.installed ? "y" : ""}"></i>${j.label}</span><span>${j.installed ? j.count + " cookies" : "not installed"}</span></div>`).join("");
  $("site").innerHTML = s.jars.map(j => `<option value="${j.id}">${j.label}</option>`).join("");
  paintSeg("dest-uploader", s.dest.uploader);
  paintSeg("dest-restricted", s.dest.restricted);
  $("foot").textContent = `User session: ${s.session ? "connected" : "not set"} · Sudo users: ${s.sudo_count}`;
}
$("install").onclick = async () => {
  const m = $("cookiemsg"); m.className = "msg"; m.textContent = "Installing…";
  const r = await api("/api/cookies", {platform: $("site").value, text: $("paste").value});
  if (r.ok) {
    m.className = "msg ok";
    m.textContent = `✅ Installed ${r.count} cookies.` + (r.warning ? " ⚠️ " + r.warning : "");
    $("paste").value = "";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    load();
  } else { m.className = "msg err"; m.textContent = "❌ " + r.error; }
};
[["dest-uploader", "uploader"], ["dest-restricted", "restricted"]].forEach(([id, feature]) => {
  document.querySelectorAll("#" + id + " button").forEach(b => b.onclick = async () => {
    const m = $("destmsg"); m.className = "msg"; m.textContent = "Saving…";
    const r = await api("/api/destination", {feature, dest: b.dataset.d});
    if (r.ok) { paintSeg(id, r.dest); m.className = "msg ok"; m.textContent = "✅ Saved."; }
    else { m.className = "msg err"; m.textContent = "❌ " + r.error; }
  });
});
load();
</script>
</body>
</html>
"""
