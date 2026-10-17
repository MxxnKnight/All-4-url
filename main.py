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
from core import webserver
from core.userclient import stop_user_client

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
    try:
        await idle()
    finally:
        await stop_user_client()
        await app.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
