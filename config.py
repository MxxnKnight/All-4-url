"""All-4-URL configuration.

Every value comes from an environment variable (or a local `.env` file in
development). Nothing secret is ever hard-coded here. See `.env.example`
for the full, commented list.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _int_or_none(name: str):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _int_set(name: str) -> set:
    """Space- or comma-separated ids, e.g. SUDO_USERS="123 456,789"."""
    ids = set()
    for part in (os.environ.get(name) or "").replace(",", " ").split():
        try:
            ids.add(int(part))
        except ValueError:
            pass
    return ids


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Telegram credentials (required) -------------------------------------
#: Release version (shown in /start and the control panel).
BOT_VERSION = "1.17.0"
API_ID = _int("API_ID")
API_HASH = (os.environ.get("API_HASH") or "").strip()
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

# The one and only user allowed to use this bot (your numeric Telegram id,
# get it from @userinfobot). Everyone else is ignored.
OWNER_ID = _int("OWNER_ID")

# Extra Telegram user ids allowed to use the bot exactly like the owner
# (full features). Space- or comma-separated, e.g. "111111111 222222222".
# This is the permanent home for sudo users — it survives restarts.
# /addsudo adds temporary ones (kept in data/sudo.json, lost on redeploy
# on hosts with ephemeral disks like Render free).
SUDO_USERS = _int_set("SUDO_USERS")

# --- Restricted downloader (optional) -------------------------------------
# Session string of the *user* account used to read restricted chats.
# Generate it with `python gen_session.py`. It is a password-equivalent:
# keep it only in the host's env vars, never in the repo.
SESSION_STRING = (os.environ.get("SESSION_STRING") or "").strip()

# --- Database (optional, recommended) -------------------------------------
# MongoDB for the bot's durable data (runtime sudo users + usage stats),
# so they survive redeploys on hosts with ephemeral disks. DB_NAME picks
# THIS bot's own database on the cluster — the same DB_URI may be shared
# with other bots, each keeps its own DB_NAME. Leave DB_URI empty to
# keep using the local JSON files instead.
DB_URI = (os.environ.get("DB_URI") or "").strip()
DB_NAME = (os.environ.get("DB_NAME") or "all4url").strip() or "all4url"

# --- Bandwidth guard --------------------------------------------------------
# Monthly outbound (uploads to Telegram + file-to-link serves) is what
# the host meters — Koyeb free includes 100 GB/month. The guard warns
# the owner at BANDWIDTH_WARN_PCT and pauses new download jobs at
# BANDWIDTH_STOP_PCT (streaming links keep working either way).
OUTBOUND_CAP_GB = _int("OUTBOUND_CAP_GB", 100)
OUTBOUND_CAP_BYTES = OUTBOUND_CAP_GB * 1024 ** 3
BANDWIDTH_WARN_PCT = _int("BANDWIDTH_WARN_PCT", 80)
BANDWIDTH_STOP_PCT = _int("BANDWIDTH_STOP_PCT", 95)

# --- File-to-link -----------------------------------------------------------
# BASE_URL (below) is the only thing file-to-link strictly needs: by
# default the links point back at the file message in the owner's PM.
# STORAGE_CHANNEL_ID is an OPTIONAL override: if set, files are copied
# into that private channel first (bot must be an admin, ids look like
# -1001234567890) and the links point at the stored copy, so they keep
# working even if the original PM message is deleted.
STORAGE_CHANNEL_ID = _int_or_none("STORAGE_CHANNEL_ID")

# Public URL of this deployment, e.g. https://my-app.koyeb.app
# Used to build download / stream links. No trailing slash.
BASE_URL = (os.environ.get("BASE_URL") or "").strip().rstrip("/")
# Tolerate BASE_URL being set to the panel address itself
# (…koyeb.app/app): the panel suffix belongs to PANEL_URL only.
# Without this, every generated panel button pointed at /app/app,
# which is a 404 — while / and /app checked manually look fine.
if BASE_URL.endswith("/app"):
    BASE_URL = BASE_URL[:-len("/app")].rstrip("/")

#: Full Mini App control-panel URL ("" when BASE_URL is unset).
PANEL_URL = (BASE_URL + "/app") if BASE_URL else ""

# Port the small web server listens on (Koyeb injects PORT automatically).
PORT = _int("PORT", 8080)

# How long file-to-link URLs stay valid.
LINK_EXPIRY_HOURS = _int("LINK_EXPIRY_HOURS", 72)

# --- Terabox (optional) -----------------------------------------------------
# The `ndus` cookie of a logged-in Terabox account. See README for how to
# copy it out of your browser. Terabox links fail without it.
TERABOX_NDUS = (os.environ.get("TERABOX_NDUS") or "").strip()

# --- Behaviour ---------------------------------------------------------------
# Where downloads land before being uploaded. On Koyeb this is ephemeral
# disk and every job deletes its files as soon as it finishes.
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR") or os.path.join(BASE_DIR, "downloads")

# Netscape cookies.txt (Instagram & co.), if the owner provided one.
COOKIES_FILE = os.environ.get("COOKIES_FILE") or os.path.join(BASE_DIR, "cookies.txt")

# Hard cap on simultaneous download/upload jobs. The Koyeb free nano has
# ~512 MB RAM and 0.1 vCPU, so 2 is the sensible ceiling.
MAX_JOBS = max(1, _int("MAX_JOBS", 2))

# Safety ceiling for a single file, in bytes. Telegram bot uploads are
# limited to 2 GB anyway; keep a little headroom below that.
MAX_FILE_SIZE = _int("MAX_FILE_SIZE", 2_000_000_000)

# Split downloads (/sdl, core/segmenter.py): an oversized direct-link
# VIDEO is cut into watchable parts instead of being refused. Each
# part obeys BOTH caps; the whole file must fit under the total cap.
SEGMENT_MAX_MIN = max(1, _int("SEGMENT_MAX_MIN", 45))
SEGMENT_MAX_MB = max(50, _int("SEGMENT_MAX_MB", 1000))
SEGMENT_MAX_TOTAL_GB = max(1, _int("SEGMENT_MAX_TOTAL_GB", 8))
SEGMENT_MAX_TOTAL_BYTES = SEGMENT_MAX_TOTAL_GB * 1000 * 1000 * 1000

# Biggest message range a single /batch may expand (first-link..last-link).
BATCH_RANGE_LIMIT = max(2, _int("BATCH_RANGE_LIMIT", 100))


def cookies_path() -> str | None:
    """Path to a usable cookies.txt, or None if the owner never set one up."""
    return COOKIES_FILE if COOKIES_FILE and os.path.isfile(COOKIES_FILE) else None


def missing_core() -> list[str]:
    """Names of required env vars that are missing (for a friendly startup log)."""
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not OWNER_ID:
        missing.append("OWNER_ID")
    return missing
