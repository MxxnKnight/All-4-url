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
BOT_VERSION = "1.51.15"
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
# a host meters. Koyeb free included 100 GB/month; Hugging Face
# publishes no egress cap, so the default here is a runaway safety
# (1 TB), not a real constraint. The guard still warns the owner at
# BANDWIDTH_WARN_PCT and pauses new download jobs at
# BANDWIDTH_STOP_PCT (streaming links keep working either way).
OUTBOUND_CAP_GB = _int("OUTBOUND_CAP_GB", 1000)
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

# Port the small web server listens on. Hosts set PORT themselves
# (Koyeb injects it; HF Spaces route 7860, so that is the default).
PORT = _int("PORT", 7860)

# How long file-to-link URLs stay valid.
LINK_EXPIRY_HOURS = _int("LINK_EXPIRY_HOURS", 72)

# --- Terabox (optional) -----------------------------------------------------
# The `ndus` cookie of a logged-in Terabox account. See README for how to
# copy it out of your browser. Terabox links fail without it.
TERABOX_NDUS = (os.environ.get("TERABOX_NDUS") or "").strip()
# The `BDUSS` cookie of the same account (Baidu's login cookie).
# Either cookie alone enables the cookie route; when both are set,
# both are sent (BDUSS accompanies ndus, it does not replace it).
TERABOX_BDUSS = (os.environ.get("TERABOX_BDUSS") or "").strip()

# --- Reddit OAuth (optional) --------------------------------------------------
# Client ID + secret of a Reddit "script" app (reddit.com/prefs/apps).
# The reddit_oauth extractor uses them for app-only API access, so
# Reddit posts download without a cookie jar. When either is empty
# the extractor stays off and Reddit flows through gallery-dl as
# before. Both are secrets: Space Secrets, never the repo.
REDDIT_CLIENT_ID = (os.environ.get("REDDIT_CLIENT_ID") or "").strip()

# TMDB (v1.44.0): optional, for real movie/show posters as video
# thumbnails. Either a v3 API key or a v4 read-access token works;
# without either, thumbnails are video frames as before. Both are
# free from themoviedb.org (account Settings > API).
TMDB_API_KEY = (os.environ.get("TMDB_API_KEY") or "").strip()
TMDB_ACCESS_TOKEN = (os.environ.get("TMDB_ACCESS_TOKEN") or "").strip()
REDDIT_CLIENT_SECRET = (os.environ.get("REDDIT_CLIENT_SECRET") or "").strip()

# --- Behaviour ---------------------------------------------------------------
# Where downloads land before being uploaded. On Koyeb this is ephemeral
# disk and every job deletes its files as soon as it finishes.
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR") or os.path.join(BASE_DIR, "downloads")

# Netscape cookies.txt (Instagram & co.), if the owner provided one.
COOKIES_FILE = os.environ.get("COOKIES_FILE") or os.path.join(BASE_DIR, "cookies.txt")

# Hard cap on simultaneous download/upload jobs. Downloads are
# mostly waiting on the network, not the CPU, so 4 fits the HF
# Space (2 CPU, 16 GB RAM) where the old Koyeb nano (0.1 vCPU)
# only managed 2. Rate limits still respected: every send goes
# through the FloodWait retry (core/sender.py), media groups and
# zip sends pace 1 s between messages, only one oversize
# download+split runs at a time (core/splitter.py BIG_FILE_LOCK),
# only one browser scan runs at a time (BROWSER_LOCK), and the
# monthly outbound guard still pauses new jobs at 95% of cap.
MAX_JOBS = max(1, _int("MAX_JOBS", 4))

# Safety ceiling for a single file, in bytes. Telegram bot uploads are
# limited to 2 GB anyway; keep a little headroom below that.
MAX_FILE_SIZE = _int("MAX_FILE_SIZE", 2_000_000_000)

# Split downloads (/vidsplit, core/segmenter.py): an oversized direct-link
# VIDEO is cut into watchable parts instead of being refused. Each
# part obeys BOTH caps; the whole file must fit under the total cap.
# SEGMENT_MAX_MB is the owner's knob for part size (Space variable):
# 1900 gives the fewest parts, 500 gives smaller ones. The ceiling
# is 1900, not 2000 — a part must stay under Telegram's 2 GB cap
# with margin, and the floor is 50 so a part is never silly-small.
SEGMENT_MAX_MIN = max(1, _int("SEGMENT_MAX_MIN", 45))
SEGMENT_MAX_MB = min(1900, max(50, _int("SEGMENT_MAX_MB", 1000)))
SEGMENT_MAX_TOTAL_GB = max(1, _int("SEGMENT_MAX_TOTAL_GB", 8))
SEGMENT_MAX_TOTAL_BYTES = SEGMENT_MAX_TOTAL_GB * 1000 * 1000 * 1000

# Live recording (/live, core/live.py): a live stream is cut into
# standalone MP4 parts of this many minutes each, sent as they
# finish. The segment muxer cuts by time only — there is no
# byte-size option — so this is the only knob. 5-60 minutes;
# 15 keeps each part a few hundred MB at live bitrates, far
# under Telegram's 2 GB cap.
LIVE_SEGMENT_MIN = min(60, max(5, _int("LIVE_SEGMENT_MIN", 15)))
LIVE_SEGMENT_SECONDS = LIVE_SEGMENT_MIN * 60

# Parallel downloads (core/extractors/direct.py): direct links of
# 20 MB or more download in 4 simultaneous Range chunks instead of
# one stream, when the host supports Range. Any failure falls back
# to the single-stream download, so a host that dislikes it just
# downloads slowly instead of failing. Space variable, 1 = on
# (default), 0 = off — every download single-stream.
PARALLEL_DOWNLOADS = _int("PARALLEL_DOWNLOADS", 1) != 0

# Byte-split downloads (core/splitter.py): a finished download over
# MAX_FILE_SIZE is chopped into near-equal .001/.002 parts of at
# most ~1.95 GB each instead of being refused, then sent as
# documents. Parts are NOT playable —
# they must be joined back (7-Zip, or `cat`) before watching, unlike
# /vidsplit's time segments. The split briefly holds the file AND its
# parts (2x on disk), so the total cap stays well under the host's
# disk (50 GB on HF).
SPLIT_PART_BYTES = max(100_000_000, _int("SPLIT_PART_BYTES", 1_950_000_000))
SPLIT_MAX_TOTAL_GB = max(1, _int("SPLIT_MAX_TOTAL_GB", 20))
SPLIT_MAX_TOTAL_BYTES = SPLIT_MAX_TOTAL_GB * 1000 * 1000 * 1000

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
