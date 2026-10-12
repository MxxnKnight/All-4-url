"""The *user* client used by the restricted downloader.

Bots cannot fetch content from chats that turned on "restrict saving",
but a normal user account that is a member of the chat can download it
via MTProto. So the bot keeps a second client, logged in as the owner
with a session string, started lazily on first use.
"""

import asyncio

from pyrogram import Client

import config

_client: Client | None = None
_lock = asyncio.Lock()
_started = False


class SessionNotConfigured(Exception):
    pass


async def get_user_client() -> Client:
    """Return the started user client, starting it on first call."""
    global _client, _started
    if _started and _client is not None:
        return _client
    async with _lock:
        if _started and _client is not None:
            return _client
        if not config.SESSION_STRING:
            raise SessionNotConfigured(
                "SESSION_STRING is not set, so the restricted downloader "
                "has no user account to read with. Run "
                "`python gen_session.py` once on your computer, then add "
                "the printed string as the SESSION_STRING env var and "
                "restart the bot. Treat it like a password."
            )
        _client = Client(
            name="all4url_user",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=config.SESSION_STRING,
            workers=30,
            sleep_threshold=120,
            in_memory=True,
        )
        await _client.start()
        _started = True
        return _client


async def stop_user_client() -> None:
    global _client, _started
    if _client is not None and _started:
        try:
            await _client.stop()
        except Exception:
            pass
    _client, _started = None, False
