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
we only ever hold one 1 MiB Telegram chunk at a time, which is what
the free-tier host needs.
"""

import asyncio
import logging
import mimetypes
from urllib.parse import quote

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

import config
from core import stats
from core.linktoken import TokenError, parse_token

log = logging.getLogger("all4url.web")

_bot = None  # set by main.py via bind_bot()

#: stream_media's chunk size, in bytes — exact, verified in the lib source.
TG_CHUNK = 1024 * 1024


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
        status_code = 200
        if size:
            headers["Content-Length"] = str(size)
    else:
        start, end = byte_range
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)

    response = web.StreamResponse(status=status_code, headers=headers)
    await response.prepare(request)
    served = 0
    if request.method != "HEAD":
        try:
            if size:
                lo = byte_range[0] if byte_range else 0
                hi = byte_range[1] if byte_range else size - 1
                served, complete = await _stream_window(
                    response, message, size, lo, hi, request)
                if not complete:
                    # Never leave the player hanging on a
                    # Content-Length we could not fill: close the
                    # connection so it sees a definite error and can
                    # retry the seek itself.
                    log.warning("stream incomplete: %d of %d bytes sent",
                                served, hi - lo + 1)
                    response.force_close()
            else:
                # Unknown size: no length was promised. Still fetched
                # one chunk at a time (see _stream_window): a
                # whole-file pass hangs the same way past chunk one.
                chunk_off = 0
                while True:
                    if _client_gone(request):
                        raise asyncio.CancelledError
                    raw = await _fetch_chunk_with_retries(
                        message, chunk_off, size=0)
                    if not raw:
                        break
                    await response.write(raw)
                    served += len(raw)
                    if len(raw) < TG_CHUNK:
                        break  # short chunk is the file tail
                    chunk_off += 1
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.warning("stream failed: %s", exc)
            response.force_close()
    try:
        await response.write_eof()
    except Exception:
        pass
    stats.record("served", served)
    return response


#: Attempts per Telegram chunk before a window is declared incomplete.
STREAM_ATTEMPTS = 4
#: Seconds to wait for one chunk before giving up on that attempt.
#: A single chunk normally arrives in ~1 s on the live server; 20 s
#: is generous, and it bounds a hung fetch instead of letting the
#: player wait until the ~100 s proxy timeout kills the connection.
CHUNK_TIMEOUT = 20


def _client_gone(request) -> bool:
    """True when the player has already hung up on this request.

    Checked between chunks and before each resume attempt, so an
    abandoned seek stops fetching instead of competing with the
    player's new seek for Telegram slots and outbound bandwidth.
    A transport that is merely slow (not closing) is NOT gone.
    """
    if request is None:
        return False
    transport = request.transport
    return transport is None or transport.is_closing()


async def _fetch_one_chunk(message, chunk_off: int,
                           timeout: float = CHUNK_TIMEOUT) -> bytes:
    """One Telegram chunk (chunk_off), via the main session only.

    `stream_media(limit=1)` never needs the library's parallel pool
    (pool only kicks in for multi-chunk windows), and that matters:
    live Range tests on 2026-10-25 proved every multi-chunk window
    delivers exactly its first chunk and then hangs — single-chunk
    windows work for every offset tested (0, 1, 47, 152). So this
    code never asks for more than one chunk per call.
    Raises on timeout (bounded by `timeout`, never an open-ended
    hang) or on a library error; returns b"" only past the end.
    """
    gen = _bot.stream_media(message, offset=chunk_off, limit=1)
    try:
        return await asyncio.wait_for(gen.__anext__(), timeout=timeout)
    except StopAsyncIteration:
        return b""
    finally:
        try:
            await gen.aclose()
        except Exception:
            pass


async def _fetch_chunk_with_retries(message, chunk_off: int,
                                    size: int) -> bytes:
    """One chunk, retried up to STREAM_ATTEMPTS times.

    A short chunk in the middle of a known-size file is treated as
    a failed fetch (writing it would leave a hole, since the
    library advances a full chunk past it) and retried, not written.
    Raises the last error when every attempt fails.
    """
    last_exc: Exception | None = None
    for attempt in range(STREAM_ATTEMPTS):
        try:
            raw = await _fetch_one_chunk(message, chunk_off)
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as exc:
            last_exc = exc
        else:
            if not raw:
                last_exc = RuntimeError("empty chunk")
            elif size and len(raw) < TG_CHUNK \
                    and chunk_off * TG_CHUNK + len(raw) < size:
                last_exc = RuntimeError("short mid-file chunk")
            else:
                return raw
        log.warning("chunk %d fetch failed (attempt %d/%d): %s",
                    chunk_off, attempt + 1, STREAM_ATTEMPTS, last_exc)
        if attempt + 1 < STREAM_ATTEMPTS:
            await asyncio.sleep(0.2)
    raise RuntimeError(f"chunk {chunk_off} failed after "
                       f"{STREAM_ATTEMPTS} attempts: {last_exc}")


async def _stream_window(response: web.StreamResponse, message,
                         size: int, lo: int, hi: int, request=None):
    """Deliver bytes [lo, hi] of the message's media to `response`.

    Returns (bytes_written, complete). Chunks are fetched one at a
    time (see _fetch_one_chunk): a multi-chunk `stream_media` call
    hangs after its first chunk on the live server, and the old
    resume logic could not help because it only runs when a pass
    *ends* — a hung pass never ends, so the player waited until
    the ~100 s proxy timeout. One-chunk calls with a per-chunk
    timeout and retry turn that hang into, at worst, a fast
    definite failure (force-close in _serve) that the player can
    retry, instead of a stuck seek. RAM stays flat: one 1 MiB
    chunk in memory at a time, written whole (slicing it only
    added aiohttp write/drain calls on a 0.1 vCPU box).
    """
    pos = lo  # next absolute byte the client is still owed
    served = 0
    while pos <= hi:
        if _client_gone(request):
            raise asyncio.CancelledError
        chunk_off, to_skip = divmod(pos, TG_CHUNK)
        try:
            raw = await _fetch_chunk_with_retries(message, chunk_off,
                                                  size)
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception:
            return served, False
        chunk = raw[to_skip:] if to_skip < len(raw) else b""
        if not chunk:
            return served, False
        chunk = chunk[:hi - pos + 1]
        await response.write(chunk)
        served += len(chunk)
        pos += len(chunk)
        if _client_gone(request):
            raise asyncio.CancelledError
    return served, True


async def _download(request: web.Request) -> web.Response:
    return await _serve(request, inline=False)


async def _stream(request: web.Request) -> web.Response:
    return await _serve(request, inline=True)


def make_app() -> web.Application:
    # 32 MB: one upload chunk is 16 MB; the default 1 MB cap would
    # reject every chunked upload with a 413.
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    # NOTE: aiohttp auto-registers HEAD for every GET route, so players
    # can probe a link's headers; _serve answers HEAD with headers only.
    from core import webapp
    webapp.add_routes(app)
    from core import upage
    upage.add_routes(app)
    app.router.add_get("/d/{token}", _download)
    app.router.add_get("/stream/{token}", _stream)
    return app


class _SilentAccessLog(AbstractAccessLogger):
    """Access logger that logs nothing — the upload page must not
    leave a trail of who connected, from where, or when.

    NOTE (learned the hard way, v1.15.0): AppRunner instantiates
    this class itself with (logger, log_format) — so do NOT override
    __init__ with no arguments, and do NOT pass an instance to
    TCPSite (modern aiohttp rejects that kwarg outright). The class
    is handed to AppRunner via access_log_class; log() stays a no-op,
    so the logger it is given is never written to.
    """

    def log(self, request, response, time):
        pass


async def start_web_server() -> web.AppRunner:
    from core import upage
    upage.startup_cleanup()
    runner = web.AppRunner(make_app(), access_log_class=_SilentAccessLog)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("web server listening on port %s", config.PORT)
    return runner
