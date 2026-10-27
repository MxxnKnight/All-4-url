""" /pipe — stream a direct file straight to Telegram, no disk.

The normal uploader downloads a whole file to Koyeb's 2 GB disk
and then uploads it. A pipe does neither: it hands Telegram a
file-like object that reads the source over HTTP, chunk by
chunk, as Telegram asks for it. Disk usage is zero (plus a
few MB of in-flight buffers); RAM is one in-flight chunk.

How it plugs into Telegram (verified against WzGram 3.1.5's
save_file, which is what send_document uses under the hood):
  * save_file accepts a BinaryIO (io.IOBase, not text).
  * It calls fp.seek(0, SEEK_END) + fp.tell() for the size,
    then fp.seek(0), then fp.read(batch) in a worker thread,
    sequentially, in batches up to 4 MB, split into 512 KB
    MTProto parts. No concurrent reads, no other seeks in the
    normal path (a resume seek to part*512KB is the only other
    one, and it starts at 0 for a fresh upload).
  * It derives the filename from fp.name, or from the
    file_name= argument to send_document (we pass both).
  * Bots are capped at 2000 MiB by save_file itself; the bot's
    own MAX_FILE_SIZE (2,000,000,000) is checked first, in
    probe(), so the user gets a plain message, not a library
    error.

RemoteFile (below) is written for exactly that contract:
  * seekable/tellable, size known upfront (probe refuses
    unknown sizes — MTProto needs total parts upfront).
  * read() is blocking and thread-safe (a lock serialises
    the executor thread's reads), and it fetches the source
    with HTTP Range requests in 64 MB windows: one open-ended
    stream would die on a slow source and lose the whole
    upload, while a window that ends exactly at a window
    boundary is re-opened transparently on the next read.
  * A stream that ends early *inside* a window raises
    OSError instead of returning b"" — returning b"" would
    make save_file think the file ended and send a truncated
    file as if it were complete. That is the one failure mode
    a pipe must never silently produce.
  * urllib (not aiohttp) is used deliberately: read() runs in
    a blocking executor thread, and urllib respects the
    environment proxy on a dev box while going direct on
    Koyeb, where no proxy is set.

Scope, stated plainly (v1):
  * Direct HTTP file links only: one file, known size,
    ≤ Telegram's cap. Playlists (.m3u8/.mpd) are refused —
    HLS is many segments, not one stream, and needs the
    ffmpeg pipe (see the reply to the owner's question) or
    yt-dlp, not this chunk pipe.
  * Sent as a document (not as a video with a thumbnail):
    a thumbnail needs ffprobe on a file, which a pipe does
    not have. Telegram clients still play mp4 documents
    inline; they just arrive without a custom thumbnail.
  * If the source dies mid-upload, the pipe fails and there
    is no partial message in the chat — the user retries
    with /pipe again (if the source link is still alive) or
    with the normal disk uploader.
"""

import io
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

import config
from core.extractors.direct import BROWSER_UA, filename_from
from core.utils import human_bytes

#: One Range window: small enough that a dropped connection
#: loses at most this much work, large enough that a 2 GB file
#: needs only ~32 windows.
WINDOW_BYTES = 64 * 1024 * 1024
#: Per-read socket timeout: slow sources (FileSnag at ~50 KB/s)
#: take far longer than 30 s to fill a 4 MB batch.
READ_TIMEOUT = 120


class PipeError(Exception):
    """Message safe to show the owner."""


def _is_playlist(url: str) -> bool:
    import os
    return os.path.splitext(urlparse(url).path)[1].lower() in (".m3u8", ".mpd")


def probe(url: str) -> dict:
    """Probe a direct link for /pipe. Returns {url, size, filename}.

    Blocking (urllib) — call it via run_blocking from async code.
    Raises PipeError for anything that cannot be piped.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        raise PipeError("that isn't a direct http(s) link")
    if "t.me/" in url or "telegram.me/" in url:
        raise PipeError("Telegram links go to the restricted downloader, "
                        "not /pipe")
    if _is_playlist(url):
        raise PipeError(
            "that's an HLS/DASH playlist, not a single file — /pipe "
            "can't stream a playlist. Send the page link to the bot "
            "instead, or ask about the ffmpeg pipe for HLS.")
    headers = {"User-Agent": BROWSER_UA}
    # HEAD first; some GET-signed hosts reject HEAD, so a HEAD
    # failure falls through to a 1-byte Range GET, which is the
    # signed method and also proves Range support.
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status < 400:
                size = int(resp.headers.get("Content-Length") or 0)
                if size:
                    _check_size(size)
                    return {"url": resp.geturl(), "size": size,
                            "filename": filename_from(
                                resp.geturl(), resp.headers)}
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            url, headers={**headers, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status >= 400:
                raise PipeError(
                    f"the source answered {resp.status} — the link "
                    f"may be dead, expired, or locked to another IP")
            if resp.status == 206:
                content_range = resp.headers.get("Content-Range", "")
                try:
                    size = int(content_range.rsplit("/", 1)[1])
                except (IndexError, ValueError):
                    size = 0
            else:
                size = int(resp.headers.get("Content-Length") or 0)
            if not size:
                raise PipeError(
                    "couldn't determine the file size — /pipe needs "
                    "a known size to tell Telegram how many parts "
                    "to expect")
            _check_size(size)
            return {"url": resp.geturl(), "size": size,
                    "filename": filename_from(resp.geturl(), resp.headers)}
    except PipeError:
        raise
    except urllib.error.HTTPError as exc:
        raise PipeError(
            f"the source answered {exc.code} — the link may be "
            f"dead, expired, or locked to another IP")
    except Exception as exc:
        raise PipeError(f"couldn't reach the source ({exc})")


def _check_size(size: int) -> None:
    # Telegram's cap only — deliberately NOT the bot's disk
    # check (check_size in utils also refuses when free disk is
    # short, which is exactly what a pipe does not need).
    if size > config.MAX_FILE_SIZE:
        raise PipeError(
            f"that file is {human_bytes(size)} — over Telegram's "
            f"{human_bytes(config.MAX_FILE_SIZE)} cap for bots, "
            f"so even a pipe can't send it as one file. "
            f"Use /sdl for a split upload instead.")


class RemoteFile(io.RawIOBase):
    """A seekable, blocking, Range-backed view of a remote file.

    Only sequential reads happen in practice (see module
    docstring), but seek() to any offset works by closing the
    current window and opening a new Range at that offset.
    """

    def __init__(self, url: str, size: int, filename: str,
                 headers: dict | None = None):
        self.url = url
        self._size = int(size)
        self.name = filename
        self._headers = {"User-Agent": BROWSER_UA}
        if headers:
            self._headers.update(headers)
        self._pos = 0
        self._stream = None
        self._stream_end = 0  # exclusive end of the current window
        self._lock = threading.Lock()
        self._closed_flag = False

    # -- io.IOBase contract -------------------------------------

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        with self._lock:
            if whence == io.SEEK_SET:
                new = offset
            elif whence == io.SEEK_CUR:
                new = self._pos + offset
            elif whence == io.SEEK_END:
                new = self._size + offset
            else:
                raise ValueError(f"bad whence {whence}")
            new = max(0, min(self._size, new))
            if new != self._pos:
                self._close_stream_locked()
            self._pos = new
            return self._pos

    def close(self) -> None:
        with self._lock:
            self._close_stream_locked()
            self._closed_flag = True
        super().close()

    # -- internals ----------------------------------------------

    def _close_stream_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._stream_end = 0

    def _open_window_locked(self) -> None:
        end = min(self._size, self._pos + WINDOW_BYTES)
        req = urllib.request.Request(
            self.url,
            headers={**self._headers,
                     "Range": f"bytes={self._pos}-{end - 1}"})
        try:
            self._stream = urllib.request.urlopen(req, timeout=READ_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise OSError(
                f"source answered {exc.code} while piping "
                f"(byte {self._pos} of {self._size})")
        except Exception as exc:
            raise OSError(f"source unreachable while piping: {exc}")
        self._stream_end = end
        # A server that ignores Range answers 200 with the whole
        # file from byte 0 — that would corrupt the upload, so
        # refuse it instead of silently sending wrong bytes.
        if getattr(self._stream, "status", 0) not in (200, 206):
            self._close_stream_locked()
            raise OSError(
                f"source answered {self._stream.status} while piping")
        if self._stream.status == 200 and self._pos != 0:
            self._close_stream_locked()
            raise OSError(
                "source ignored the Range request — /pipe needs "
                "Range support for seeks past byte 0")

    def readinto(self, b) -> int | None:
        data = self.read(len(b))
        if not data:
            return 0
        b[:len(data)] = data
        return len(data)

    def read(self, size: int = -1) -> bytes:
        with self._lock:
            if self._closed_flag:
                raise ValueError("I/O operation on closed file")
            if size is None or size < 0:
                # Pyrogram never does this (it always passes a
                # batch size), but support it without blowing RAM
                # by looping in 1 MB steps.
                chunks = []
                remaining = self._size - self._pos
                while remaining > 0:
                    chunk = self._read_one(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks)
            if self._pos >= self._size:
                return b""
            return self._read_one(size)

    def _read_one(self, size: int) -> bytes:
        # Caller holds the lock; self._pos < self._size on entry.
        while True:
            if self._stream is None:
                self._open_window_locked()
            data = self._stream.read(size)
            if data:
                self._pos += len(data)
                return data
            # Stream ended. At a window boundary with more file
            # left, that is expected — open the next window.
            if self._pos == self._stream_end and self._pos < self._size:
                self._close_stream_locked()
                continue
            if self._pos >= self._size:
                return b""
            # Ended early inside a window: never pretend the file
            # is complete — a truncated upload is worse than a
            # failed one.
            self._close_stream_locked()
            raise OSError(
                f"source ended early at {human_bytes(self._pos)} "
                f"of {human_bytes(self._size)} — the link may have "
                f"expired mid-upload")
