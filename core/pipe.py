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

FFmpeg pipe (HLS/DASH, second half of this module):
  A playlist is not a file, so RemoteFile cannot pipe it:
  there is no single byte range to read, and no size to tell
  Telegram. ffmpeg solves the first half — it reads the
  playlist, fetches the segments one by one, and remuxes
  them (stream-copy, no re-encode) into one fragmented mp4
  on stdout. The second half, the size, is solved by running
  ffmpeg twice: a measurement pass that counts stdout bytes
  (and discards them), then an upload pass that streams the
  same command to Telegram with the measured size. That is
  why HLS/DASH pipes download twice and upload once:
  inbound is free on Koyeb, outbound is not, so the cost is
  time, not bandwidth cap — but the source must be
  re-fetchable (VOD only, never live: a live playlist has
  no end, no size, and its segments change between passes).
  The upload pass enforces the measured size exactly: if
  ffmpeg produces more bytes than measured, or ends early,
  the upload fails instead of sending a truncated file that
  Telegram would accept as complete (save_file returns
  InputFile with parts computed from the size, regardless
  of how many parts were actually sent).
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


def is_playlist_url(url: str) -> bool:
    """True for .m3u8/.mpd links (the /pipe handler's playlist route)."""
    return _is_playlist(url or "")


def _is_playlist_content_type(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "mpegurl" in ct or "dash+xml" in ct or "x-mpegurl" in ct


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
        # The /pipe handler routes these to the ffmpeg pipe
        # before calling probe(); a direct caller gets a plain
        # refusal, not a playlist file sent as a document.
        raise PipeError(
            "that's an HLS/DASH playlist, not a single file — "
            "the /pipe handler pipes playlists through ffmpeg, "
            "it does not send the playlist file itself.")
    headers = {"User-Agent": BROWSER_UA}
    # HEAD first; some GET-signed hosts reject HEAD, so a HEAD
    # failure falls through to a 1-byte Range GET, which is the
    # signed method and also proves Range support.
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status < 400:
                if _is_playlist_content_type(
                        resp.headers.get("Content-Type", "")):
                    return {"is_playlist": True, "url": resp.geturl()}
                size = int(resp.headers.get("Content-Length") or 0)
                if size:
                    _check_size(size)
                    return {"url": resp.geturl(), "size": size,
                            "filename": filename_from(
                                resp.geturl(), resp.headers)}
    except PipeError:
        raise
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
            if _is_playlist_content_type(
                    resp.headers.get("Content-Type", "")):
                return {"is_playlist": True, "url": resp.geturl()}
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


# ----------------------------------------------------------------------
# FFmpeg pipe: HLS (.m3u8) and DASH (.mpd) -> fragmented mp4 -> Telegram
# ----------------------------------------------------------------------
# Blocking code (subprocess + urllib), like the rest of this module —
# call it via run_blocking from async code.

import re as _re
import subprocess as _subprocess
from collections import deque as _deque
from urllib.parse import urljoin as _urljoin

#: HLS playlist fetch cap: a variant playlist for a long video is
#: tens of KB; a master is smaller. Anything over this is not a
#: playlist we can parse safely in memory.
_PLAYLIST_MAX_BYTES = 5 * 1024 * 1024


def _sanitize(text: str) -> str:
    """Strip URLs (which may carry signed tokens) from ffmpeg output."""
    return _re.sub(r"https?://[^\s'\"<>]+", "[source]", text or "")


def _fetch_playlist(url: str) -> tuple[str, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status >= 400:
                raise PipeError(
                    f"the playlist source answered {resp.status}")
            raw = resp.read(_PLAYLIST_MAX_BYTES + 1)
    except PipeError:
        raise
    except urllib.error.HTTPError as exc:
        raise PipeError(
            f"the playlist source answered {exc.code} — the link "
            f"may be dead, expired, or locked to another IP")
    except Exception as exc:
        raise PipeError(f"couldn't fetch the playlist ({exc})")
    if len(raw) > _PLAYLIST_MAX_BYTES:
        raise PipeError("that playlist is too large to parse here")
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        raise PipeError("that playlist isn't readable text")
    return resp.geturl(), text


def _hls_duration(text: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                total += float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pass
    return total


def _parse_iso8601_duration(text: str) -> float:
    # PT1H2M3.5S, PT45S, PT2M — the forms MPDs actually use.
    m = _re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?"
        r"(?:(\d+(?:\.\d+)?)S)?", text.strip())
    if not m:
        return 0.0
    h, mi, s = (float(v) if v else 0.0 for v in m.groups())
    return h * 3600 + mi * 60 + s


def inspect_playlist(url: str) -> dict:
    """Inspect an HLS/DASH playlist for /pipe.

    Returns {kind, url, duration, filename} where url is the URL
    ffmpeg should actually read (the max-bandwidth variant for a
    master HLS playlist, the original URL otherwise), duration
    is seconds (0 when unknown), and filename ends in .mp4.
    Raises PipeError for live playlists, unparseable playlists,
    and anything that is not HLS/DASH.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        raise PipeError("that isn't a direct http(s) link")
    if "t.me/" in url or "telegram.me/" in url:
        raise PipeError("Telegram links go to the restricted downloader, "
                        "not /pipe")
    final_url, text = _fetch_playlist(url)
    stripped = text.lstrip("\ufeff \t\r\n")
    base_name = final_url.rsplit("/", 1)[-1].split("?", 1)[0]
    stem = base_name.rsplit(".", 1)[0] if "." in base_name else base_name
    filename = (stem or "video") + ".mp4"
    if stripped.startswith("#EXTM3U"):
        variants = []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:"):
                bw = 0
                m = _re.search(r"BANDWIDTH=(\d+)", line)
                if m:
                    bw = int(m.group(1))
                # The variant URI is the next non-empty, non-tag line.
                for nxt in lines[i + 1:]:
                    if nxt and not nxt.startswith("#"):
                        variants.append((bw, _urljoin(final_url, nxt)))
                        break
        if variants:
            variants.sort(key=lambda v: v[0], reverse=True)
            _v_url, v_text = _fetch_playlist(variants[0][1])
            return _inspect_hls_variant(_v_url, v_text, filename)
        return _inspect_hls_variant(final_url, text, filename)
    if stripped.startswith("<?xml") or "<MPD" in text[:2000]:
        m = _re.search(r'type\s*=\s*"([^"]+)"', text[:2000])
        if m and m.group(1).lower() == "dynamic":
            raise PipeError(
                "that's a live DASH stream — it has no end and no "
                "size, so it can't be piped as one file")
        m = _re.search(
            r'mediaPresentationDuration\s*=\s*"([^"]+)"', text[:4000])
        duration = _parse_iso8601_duration(m.group(1)) if m else 0.0
        return {"kind": "dash", "url": final_url,
                "duration": duration, "filename": filename}
    raise PipeError(
        "that link isn't an HLS or DASH playlist I recognise — "
        "send the .m3u8/.mpd link itself, not a video page")


def _inspect_hls_variant(url: str, text: str, filename: str) -> dict:
    is_vod = "#EXT-X-PLAYLIST-TYPE:VOD" in text
    has_end = "#EXT-X-ENDLIST" in text
    if not is_vod and not has_end:
        raise PipeError(
            "that's a live HLS stream — it has no end and no "
            "size, so it can't be piped as one file. Live "
            "streams need a recorder, not /pipe.")
    return {"kind": "hls", "url": url,
            "duration": _hls_duration(text), "filename": filename}


def ffmpeg_cmd(url: str) -> list[str]:
    """ffmpeg: playlist -> fragmented mp4 on stdout, stream-copy.

    -c copy means no decode/re-encode: CPU is remuxing only,
    which is why this fits Koyeb's 0.1 vCPU. Fragmented mp4
    (empty_moov) is required because a normal mp4 needs to
    seek back to write its header — stdout cannot seek.
    -map with '?' keeps audio-only and video-only sources
    working instead of failing on a missing stream.
    """
    return ["ffmpeg", "-v", "error",
            "-user_agent", BROWSER_UA,
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", url,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c", "copy",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-"]


class _StderrDrain(threading.Thread):
    """Drain a subprocess's stderr into the last few lines only.

    ffmpeg's stderr must be read continuously or a full pipe
    buffer blocks it; keeping only the tail bounds memory and
    never touches disk.
    """

    def __init__(self, stream):
        super().__init__(daemon=True)
        self._stream = stream
        self.lines = _deque(maxlen=20)

    def run(self):
        try:
            for line in self._stream:
                try:
                    self.lines.append(line.decode("utf-8", "replace"))
                except Exception:
                    pass
        except Exception:
            pass

    def tail(self) -> str:
        return _sanitize("".join(self.lines).strip().splitlines()[-1]
                         if self.lines else "")


def measure_ffmpeg_output(url: str, counter: dict | None = None) -> int:
    """Run ffmpeg once and count its stdout bytes (discarded).

    Returns the byte count, which is the size the upload pass
    must reproduce exactly (see FfmpegFile). Raises PipeError
    on a non-zero ffmpeg exit or an empty output.
    """
    proc = _subprocess.Popen(ffmpeg_cmd(url),
                             stdout=_subprocess.PIPE,
                             stderr=_subprocess.PIPE)
    drain = _StderrDrain(proc.stderr)
    drain.start()
    total = 0
    try:
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if counter is not None:
                counter["bytes"] = total
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    code = proc.wait()
    drain.join(timeout=2)
    if code != 0:
        tail = drain.tail()
        raise PipeError(
            "ffmpeg couldn't read that stream"
            + (f" ({tail})" if tail else ""))
    if total == 0:
        raise PipeError("ffmpeg produced no output for that stream")
    return total


class FfmpegFile(io.RawIOBase):
    """A seekable view of ffmpeg's stdout for a playlist.

    The size is the measurement pass's count. Reads are
    limited to size - pos, so a batch never over-reads; after
    pos reaches size, one peek byte decides the outcome:
    an extra byte means the upload pass produced more than
    the measurement pass (playlist changed between passes)
    and the upload fails instead of silently truncating,
    while EOF means the sizes matched and read returns b"".
    The subprocess starts lazily on the first read, after
    save_file's seek(0) — starting it in __init__ would waste
    a whole ffmpeg run if the caller only probes the size.
    """

    def __init__(self, url: str, size: int, filename: str):
        self.url = url
        self._size = int(size)
        self.name = filename
        self._pos = 0
        self._proc = None
        self._drain = None
        self._lock = threading.Lock()
        self._closed_flag = False
        self._peeked = False

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
                self._kill_locked()
                self._pos = new
                self._peeked = False
            return self._pos

    def close(self) -> None:
        with self._lock:
            self._kill_locked()
            self._closed_flag = True
        super().close()

    def _kill_locked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
            except Exception:
                pass
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            self._proc = None
            self._drain = None

    def _start_locked(self) -> None:
        self._proc = _subprocess.Popen(
            ffmpeg_cmd(self.url),
            stdout=_subprocess.PIPE, stderr=_subprocess.PIPE)
        self._drain = _StderrDrain(self._proc.stderr)
        self._drain.start()

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
            if self._pos >= self._size:
                # Size-match peek (see class docstring): one byte
                # decides between a clean EOF and a mismatch abort.
                if self._peeked:
                    return b""
                self._peeked = True
                if self._proc is None:
                    return b""
                extra = self._proc.stdout.read(1)
                if extra:
                    self._kill_locked()
                    raise OSError(
                        "ffmpeg produced more output on the upload "
                        "pass than on the measurement pass — the "
                        "stream may have changed. Try /pipe again.")
                return b""
            if size is None or size < 0:
                size = self._size - self._pos
            else:
                size = min(size, self._size - self._pos)
            return self._read_one(size)

    def _read_one(self, size: int) -> bytes:
        if self._proc is None:
            self._start_locked()
            # A seek past 0 before the first read (the resume
            # case, normally never taken) is served by discarding
            # bytes from the start of the stream.
            remaining = self._pos
            while remaining > 0:
                chunk = self._proc.stdout.read(min(1024 * 1024,
                                                  remaining))
                if not chunk:
                    self._kill_locked()
                    raise OSError(
                        "ffmpeg ended early while skipping to "
                        f"byte {self._pos} of {self._size}")
                remaining -= len(chunk)
        data = self._proc.stdout.read(size)
        if data:
            self._pos += len(data)
            return data
        # stdout EOF before the measured size: never pretend
        # the file is complete.
        code = self._proc.poll()
        tail = self._drain.tail() if self._drain else ""
        self._kill_locked()
        if code not in (0, None):
            raise OSError(
                "ffmpeg failed mid-stream"
                + (f" ({tail})" if tail else ""))
        raise OSError(
            f"ffmpeg ended early at {human_bytes(self._pos)} "
            f"of {human_bytes(self._size)} — the stream may "
            f"have changed between passes")
