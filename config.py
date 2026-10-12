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


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Telegram credentials (required) -------------------------------------
API_ID = _int("API_ID")
API_HASH = (os.environ.get("API_HASH") or "").strip()
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

# The one and only user allowed to use this bot (your numeric Telegram id,
# get it from @userinfobot). Everyone else is ignored.
OWNER_ID = _int("OWNER_ID")

# --- Restricted downloader (optional) -------------------------------------
# Session string of the *user* account used to read restricted chats.
# Generate it with `python gen_session.py`. It is a password-equivalent:
# keep it only in the host's env vars, never in the repo.
SESSION_STRING = (os.environ.get("SESSION_STRING") or "").strip()

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
