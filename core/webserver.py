"""Tiny aiohttp server that serves file-to-link downloads/streams.

It runs next to the bot in the same process. A request carries a signed
token (see core/linktoken.py); we look up the message the token points
at (the owner's PM message by default, or a storage-channel copy when
that mode is enabled) and stream its bytes out with the bot client's
`stream_media`, so nothing is ever written to disk.

Seeking: `stream_media` takes an `offset` counted in chunks of exactly
1 MiB (verified in the library source: chunk_size = 1024*1024,
offset_bytes = offset * chunk_size). A byte Range is therefore served
byte-exact: skip whole chunks via offset, drop the leftover bytes of
the first chunk, and stop after the requested length. RAM stays flat —
we only ever hold one slice (256 KiB) at a time, which is what the
free-tier host needs.
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

#: stream_media's chunk size, in bytes — exact, verified in the lib source.
TG_CHUNK = 1024 * 1024
#: Max bytes held in RAM per HTTP write. Small on purpose: the free-tier
#: host has ~512 MB and a 2 GB file must never sit in memory.
HTTP_SLICE = 256 * 1024


def bind_bot(client) -> None:
    global _bot
    _bot = client


def _parse_range(header: str | None, size: int):
    """Parse a Range header against a known `size`.

    Returns (start, end) inclusive, or None when no (usable) Range was
    sent. Raises ValueError for malformed/unsatisfiable ranges.
    """
    if not header or size <= 0:
        return None
    header = header.strip()
    if not header.startswith("bytes="):
        raise ValueError("bad range unit")
    spec = header[len("bytes="):].strip()
    if "," in spec:
        raise ValueError("multi-range not supported")
    if spec.startswith("-"):  # suffix: the last N bytes
        count = int(spec[1:])
        if count <= 0:
            raise ValueError("bad range")
        start, end = max(0, size - count), size - 1
    elif spec.endswith("-"):  # open-ended: from N to the end
        start, end = int(spec[:-1]), size - 1
    else:
        start, end = (int(x) for x in spec.split("-", 1))
    if start < 0 or end < start or start >= size:
        raise ValueError("unsatisfiable")
    return start, min(end, size - 1)


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

    try:
        byte_range = _parse_range(request.headers.get("Range"), size)
    except ValueError:
        headers = {"Content-Range": f"bytes */{size}"} if size else {}
        return web.Response(status=416, headers=headers,
                             text="Range not satisfiable.\n")

    disposition = "inline" if inline else "attachment"
    headers = {
        "Content-Type": mime,
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}",
        "Accept-Ranges": "bytes",
    }
    if byte_range is None:
        status_code, offset, limit, remaining = 200, 0, 0, None
        if size:
            headers["Content-Length"] = str(size)
    else:
        start, end = byte_range
        remaining = end - start + 1
        skip_chunks, skip_in_chunk = divmod(start, TG_CHUNK)
        # Whole 1 MiB chunks to skip, then drop the leftover bytes of
        # the first chunk; limit trims the tail to the exact length.
        offset = skip_chunks
        limit = -(-(skip_in_chunk + remaining) // TG_CHUNK)  # ceil div
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(remaining)

    response = web.StreamResponse(status=status_code, headers=headers)
    await response.prepare(request)
    if request.method != "HEAD":
        first = True
        try:
            async for chunk in _bot.stream_media(message, offset=offset,
                                                 limit=limit):
                if not chunk:
                    continue
                if first and byte_range is not None:
                    first = False
                    chunk = chunk[skip_in_chunk:]
                if remaining is not None:
                    chunk = chunk[:remaining]
                    remaining -= len(chunk)
                # Write in small slices: never hold a whole 1 MiB
                # Telegram chunk in the HTTP buffer at once.
                for i in range(0, len(chunk), HTTP_SLICE):
                    await response.write(chunk[i:i + HTTP_SLICE])
                if remaining is not None and remaining <= 0:
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.warning("stream failed: %s", exc)
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
    # NOTE: aiohttp auto-registers HEAD for every GET route, so players
    # can probe a link's headers; _serve answers HEAD with headers only.
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
