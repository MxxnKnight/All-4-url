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
from pyrogram.types import (BotCommand,
                            BotCommandScopeAllChatAdministrators,
                            BotCommandScopeAllGroupChats,
                            BotCommandScopeAllPrivateChats,
                            BotCommandScopeChat)

import config
from core import (backup, cookies, db, settings, stats, sudo,
                  webserver)
from core.userclient import stop_user_client
from core.utils import human_bytes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("all4url")

#: The commands menu (Telegram's "/" popup). Registered with Telegram
#: on every startup, so the menu is always there with no BotFather setup.
BOT_COMMANDS = [
    BotCommand("start", "Show the main menu"),
    BotCommand("help", "How each feature works"),
    BotCommand("batch", "Batch t.me links — or a first..last range"),
    BotCommand("cancel", "Stop the current batch"),
    BotCommand("mediainfo", "File metadata: reply, file, or link"),
    BotCommand("sdl", "Split-download an oversize video link"),
    BotCommand("destination", "Upload destination: Bot PM / Saved Messages"),
    BotCommand("addsudo", "Add a sudo user (owner only)"),
    BotCommand("delsudo", "Remove a sudo user (owner only)"),
    BotCommand("sudos", "List sudo users (owner only)"),
    BotCommand("setcookies", "Install cookies.txt (owner only)"),
    BotCommand("cookies", "List cookie jars (owner only)"),
    BotCommand("updateytdlp", "Update yt-dlp now (owner only)"),
]


def _menu_variants():
    """(label, scope, language) combos Telegram resolves the "/" menu
    from. The (default, no-language) combo is the main list; every
    other combo is a place a PREVIOUS bot on this same token could
    have left commands that override ours in some chats/languages."""
    scopes = [("default", None),
              ("all_private_chats", BotCommandScopeAllPrivateChats()),
              ("all_group_chats", BotCommandScopeAllGroupChats()),
              ("all_chat_administrators",
               BotCommandScopeAllChatAdministrators()),
              ("owner_chat",
               BotCommandScopeChat(chat_id=config.OWNER_ID))]
    return [(f"{label} lang={lang or '-'}", scope, lang)
            for label, scope in scopes for lang in ("", "en")]


async def _register_commands(app: Client) -> None:
    """Sync Telegram's "/" menu with BOT_COMMANDS on every boot, and
    sweep stale command lists left by other bots in other scopes or
    languages (they shadow the main list). Logs every action."""
    want = [(c.command, c.description) for c in BOT_COMMANDS]
    try:
        current = await app.get_bot_commands()
        have = [(c.command, c.description) for c in current]
        if have == want:
            log.info("bot commands menu already up to date "
                     "(%d commands)", len(want))
        elif await app.set_bot_commands(BOT_COMMANDS):
            log.info("bot commands menu updated (%d commands): %s",
                     len(want),
                     ", ".join("/" + name for name, _ in want))
    except Exception as exc:
        log.warning("could not sync bot commands menu: %s", exc)
    for label, scope, lang in _menu_variants():
        if scope is None and lang == "":
            continue  # the main list, synced above
        try:
            stale = await app.get_bot_commands(scope=scope,
                                               language_code=lang)
            if stale:
                names = ", ".join("/" + c.command for c in stale)
                if await app.delete_bot_commands(scope=scope,
                                                 language_code=lang):
                    log.info("cleared stale bot commands [%s]: %s",
                             label, names)
        except Exception as exc:
            log.warning("could not check stale commands [%s]: %s",
                        label, exc)


async def _register_panel_button(app: Client) -> None:
    """Point the chat menu button (next to the text box) at the
    Mini App control panel — only when BASE_URL is set, since the
    panel lives on the bot's own web server."""
    if not config.BASE_URL:
        return
    try:
        from pyrogram.types import MenuButtonWebApp, WebAppInfo
        button = MenuButtonWebApp(
            text="🎛 Panel",
            web_app=WebAppInfo(url=config.BASE_URL + "/app"))
        await app.set_chat_menu_button(menu_button=button)
        # A previous bot on this token may have set a *per-chat* menu
        # button for the owner's PM, which shadows the default — set
        # that one explicitly too.
        try:
            await app.set_chat_menu_button(chat_id=config.OWNER_ID,
                                           menu_button=button)
        except Exception as exc:
            log.warning("could not set owner-chat menu button: %s", exc)
        log.info("chat menu button set to control panel (%s/app)",
                 config.BASE_URL)
    except Exception as exc:
        log.warning("could not set chat menu button: %s", exc)


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
    await _register_panel_button(app)
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
    await settings.init_from_db()

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
