"""All-4-URL — personal Telegram downloader bot.

One process, two clients' worth of work:
  * the bot client (this file, plugins/ on top of it), and
  * an aiohttp web server (core/webserver.py) for file-to-link streaming.
The restricted downloader's user client is started lazily, only when a
t.me link arrives (core/userclient.py).

Tuned for a tiny free host (Koyeb nano: ~512 MB RAM, shared vCPU):
uvloop as the event loop, few workers, and a global cap of
config.MAX_JOBS simultaneous transfers.
"""

import asyncio
import logging
import os

try:
    import uvloop

    uvloop.install()
except Exception:  # uvloop unavailable (e.g. Windows dev) — asyncio is fine
    pass

from pyrogram import Client, idle
from pyrogram.types import BotCommand

import config
from core import backup, cookies, db, stats, sudo, webserver
from core.userclient import stop_user_client
from core.utils import human_bytes

#: The commands menu (Telegram's "/" popup). Registered with Telegram
#: on every startup, so the menu is always there with no BotFather setup.
BOT_COMMANDS = [
    BotCommand("start", "Show the main menu"),
    BotCommand("help", "How each feature works"),
    BotCommand("batch", "Batch t.me links — or a first..last range"),
    BotCommand("cancel", "Stop the current batch"),
    BotCommand("mediainfo", "File metadata: reply, file, or link"),
    BotCommand("addsudo", "Add a sudo user (owner only)"),
    BotCommand("delsudo", "Remove a sudo user (owner only)"),
    BotCommand("sudos", "List sudo users (owner only)"),
    BotCommand("setcookies", "Install cookies.txt (owner only)"),
    BotCommand("cookies", "List cookie jars (owner only)"),
    BotCommand("updateytdlp", "Update yt-dlp now (owner only)"),
]


async def _register_commands(app: Client) -> None:
    try:
        if await app.set_bot_commands(BOT_COMMANDS):
            log.info("bot commands registered")
    except Exception as exc:
        log.warning("could not register bot commands: %s", exc)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("all4url")


def _bot_workers() -> int:
    """Scale workers to the box, but stay tiny on a free nano."""
    cores = os.cpu_count() or 1
    return max(4, min(32, cores * 4))


async def run() -> None:
    missing = config.missing_core()
    if missing:
        log.error("Missing required env vars: %s — see .env.example",
                  ", ".join(missing))
        raise SystemExit(1)

    app = Client(
        name="all4url",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workers=_bot_workers(),
        sleep_threshold=120,
        ipv6=False,
        plugins=dict(root="plugins"),
    )

    webserver.bind_bot(app)
    runner = await webserver.start_web_server()

    await app.start()
    me = await app.get_me()
    log.info("bot started as @%s (owner id %s)", me.username, config.OWNER_ID)
    await _register_commands(app)
    restored = await backup.restore_missing(app)
    if restored:
        log.info("restored from vault chat: %s", ", ".join(restored))
    # Durable storage: Mongo when DB_URI is set (sudo, usage, cookie
    # jars), else the local files. Runs after the vault restore so file
    # data the vault brought back can migrate up into a fresh database.
    await db.init()
    await sudo.init_from_db()
    await stats.init_from_db()
    await cookies.init_from_db()

    async def _bandwidth_alert(level: str, used: int, cap: int) -> None:
        pct = used / cap * 100 if cap else 0.0
        if level == "warn":
            text = (f"⚠️ **Bandwidth at {pct:.0f}%** — "
                    f"{human_bytes(used)} of the "
                    f"{config.OUTBOUND_CAP_GB} GB monthly outbound used. "
                    f"At {config.BANDWIDTH_STOP_PCT}% new downloads pause "
                    "until the 1st (streaming links keep working).")
        else:
            text = (f"⛔ **Bandwidth at {pct:.0f}%** — new download "
                    "jobs are now paused until the 1st. Streaming "
                    "links still work.")
        try:
            await app.send_message(config.OWNER_ID, text)
        except Exception as exc:
            log.warning("bandwidth alert failed: %s", exc)

    stats.set_alert_callback(_bandwidth_alert)
    flusher = asyncio.create_task(stats.flusher())
    try:
        await idle()
    finally:
        flusher.cancel()
        await stats.flush()
        try:
            await backup.backup_kind(app, "data/stats.json",
                                     stats.DATA_FILE)
        except Exception as exc:
            log.warning("stats backup on shutdown failed: %s", exc)
        await db.close()
        await stop_user_client()
        await app.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
