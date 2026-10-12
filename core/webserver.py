"""Tiny aiohttp server that serves file-to-link downloads/streams.

It runs next to the bot in the same process. A request carries a signed
token (see core/linktoken.py); we look up the message the token points
at (the owner's PM message by default, or a storage-channel copy when
that mode is enabled) and stream its bytes out chunk by chunk with the
bot client's `stream_media`, so nothing is ever written to disk.

Range support: HTTP clients (video players especially) ask for byte
ranges. `stream_media` accepts an `offset` counted in *chunks* of its
internal 1 MiB chunk size in this library family, not in bytes, so a
byte-exact seek cannot be built on it safely. We therefore answer a
Range request by streaming from the start with a 200 and the full
Content-Length — most players still play it (they just cannot seek) —
and say so here rather than faking a 206 with wrong offsets.
"""

import asyncio
import logging
import mimetypes
from urllib.parse import quote

from aiohttp import web

import config
from core.linktoken import TokenError, parse_token

log = logging.getLogger("all4url.web")

_bot = None  # set by main.py via bind_bot()


def bind_bot(client) -> None:
    global _bot
    _bot = client


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="All-4-URL is running.\n")


async def _serve(request: web.Request, inline: bool) -> web.Response:
    token = request.match_info["token"]
    try:
        payload = parse_token(token)
    except TokenError as exc:
        status = 410 if "expired" in str(exc) else 404
        return web.Response(status=status, text=f"Link not usable: {exc}\n")
    if _bot is None:
        return web.Response(status=503, text="Bot is not ready yet.\n")

    try:
        message = await _bot.get_messages(payload["c"], payload["m"])
    except Exception as exc:
        log.warning("get_messages failed: %s", exc)
        return web.Response(status=404, text="Stored file not found.\n")
    if message is None or getattr(message, "empty", False):
        return web.Response(status=404, text="Stored file not found.\n")

    name = payload.get("n") or "file"
    mime = payload.get("t") or mimetypes.guess_type(name)[0] \
        or "application/octet-stream"
    size = int(payload.get("s") or 0)

    disposition = "inline" if inline else "attachment"
    headers = {
        "Content-Type": mime,
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}",
        "Accept-Ranges": "none",
    }
    if size:
        headers["Content-Length"] = str(size)

    response = web.StreamResponse(status=200, headers=headers)
    await response.prepare(request)
    try:
        async for chunk in _bot.stream_media(message):
            if chunk:
                await response.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as exc:
        log.warning("stream failed: %s", exc)
    finally:
        try:
            await response.write_eof()
        except Exception:
            pass
    return response


async def _download(request: web.Request) -> web.Response:
    return await _serve(request, inline=False)


async def _stream(request: web.Request) -> web.Response:
    return await _serve(request, inline=True)


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/d/{token}", _download)
    app.router.add_get("/stream/{token}", _stream)
    return app


async def start_web_server() -> web.AppRunner:
    runner = web.AppRunner(make_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("web server listening on port %s", config.PORT)
    return runner
