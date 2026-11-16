"""Byte-split downloads that are too big for one Telegram file.

Telegram bot uploads cap at 2 GB (config.MAX_FILE_SIZE), but the host
can now hold much bigger files (50 GB disk on HF, vs 2 GB on Koyeb).
So instead of refusing a 5 GB download, the bot chops the finished
file into near-equal parts of at most ~1.95 GB — `movie.mkv.001`,
`movie.mkv.002`, … — sends each part as a document, and deletes
them from disk.

Parts are NOT playable on their own: they are chopped byte ranges
and must be joined back first (7-Zip, or `cat part1 part2 > file`)
before watching. That is the difference from /vidsplit (core/segmenter.py),
whose time segments ARE watchable videos. MoonStream already knows
`.001`/`.002` files and shows them as Part 1, Part 2.

Disk safety: a split briefly holds the file AND its parts (2x the
file on disk), so:
  * check_size(allow_split=True) only admits files up to
    SPLIT_MAX_TOTAL_BYTES, and only when 2x the file fits on disk;
  * BIG_FILE_LOCK serialises the download+split window of oversize
    jobs — two of them at once could otherwise fill the disk.
    Normal (small) jobs never touch the lock.
  * maybe_split re-checks disk room right before splitting, which
    also covers flows that never knew the size up front
    (gallery-dl, scrape).
"""

import asyncio
import os
import re
import shutil
from contextlib import asynccontextmanager

import config
from core.utils import SizeBlocked, human_bytes, run_blocking

#: One oversize download+split at a time (see module docstring).
BIG_FILE_LOCK = asyncio.Lock()

_PART_RE = re.compile(r"\.\d{3}$")
_CHUNK = 8 * 1024 * 1024
#: Keep this much disk free after a download+split.
_DISK_HEADROOM = 256 * 1024 * 1024


def is_split_part(path: str) -> bool:
    """True for `name.ext.001`-style part files (7-Zip naming)."""
    return bool(path) and bool(_PART_RE.search(os.path.basename(path)))


def needs_split(size: int | None) -> bool:
    """True when a finished file of `size` bytes must go out in parts."""
    return bool(size) and size > config.MAX_FILE_SIZE


def check_split_room(size: int) -> None:
    """Raise SizeBlocked unless the file AND its parts fit on disk."""
    try:
        free = shutil.disk_usage(config.BASE_DIR).free
    except Exception:
        return  # can't measure; let the split try
    reserved = 0
    try:
        from core import upage
        reserved = upage.pending_bytes()
    except Exception:
        reserved = 0
    if 2 * size + _DISK_HEADROOM + reserved > free:
        raise SizeBlocked(
            f"Not enough space on the server to split "
            f"{human_bytes(size)} right now — try again when the "
            f"server is less busy.")


@asynccontextmanager
async def big_file_guard(is_big: bool):
    """Hold BIG_FILE_LOCK across a download+split when `is_big`.

    Flows that know the size up front wrap their download and the
    maybe_split call in this; flows that never knew the size pass
    False and rely on maybe_split's own disk check instead.
    """
    if is_big:
        async with BIG_FILE_LOCK:
            yield
    else:
        yield


def _split_file_sync(path: str, part_size: int) -> list[str]:
    """Chop `path` into part_size pieces; returns the part paths.

    Blocking (run it through run_blocking). The original is deleted
    only after every part is written AND the part sizes add up to
    the original — a short read or a size mismatch deletes the
    partial parts instead and raises.
    """
    size = os.path.getsize(path)
    parts: list[str] = []
    try:
        with open(path, "rb") as src:
            index = 1
            remaining = size
            while remaining > 0:
                part_path = f"{path}.{index:03d}"
                to_write = min(part_size, remaining)
                written = 0
                with open(part_path, "wb") as dst:
                    while written < to_write:
                        chunk = src.read(min(_CHUNK, to_write - written))
                        if not chunk:
                            raise IOError(
                                "short read while splitting "
                                f"{os.path.basename(path)}")
                        dst.write(chunk)
                        written += len(chunk)
                parts.append(part_path)
                remaining -= to_write
                index += 1
        total = sum(os.path.getsize(p) for p in parts)
        if total != size:
            raise IOError(
                f"split parts add up to {human_bytes(total)}, not "
                f"{human_bytes(size)} — refusing to send them")
    except Exception:
        for part in parts:
            try:
                os.unlink(part)
            except OSError:
                pass
        raise
    try:
        os.unlink(path)
    except OSError:
        pass  # the job dir cleanup removes leftovers anyway
    return parts


async def maybe_split(paths: list[str], editor=None,
                      status_msg=None) -> list[str]:
    """Split every oversize file in `paths` into .001/.002 parts.

    Files within the Telegram cap pass through untouched. Raises
    SizeBlocked when a file is over the split total cap (only
    reachable for flows that never knew the size up front — the
    known-size flows refuse before downloading) or when the disk
    cannot hold the file and its parts at once.
    `editor` (ThrottledEditor) or `status_msg` gets a "Splitting…"
    note; either may be None.
    """
    out: list[str] = []
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            out.append(path)
            continue
        if not needs_split(size):
            out.append(path)
            continue
        if size > config.SPLIT_MAX_TOTAL_BYTES:
            raise SizeBlocked(
                f"{os.path.basename(path)} is {human_bytes(size)} — "
                f"over the {human_bytes(config.SPLIT_MAX_TOTAL_BYTES)} "
                f"split limit, so I can't send it even in parts.")
        check_split_room(size)
        count = (size + config.SPLIT_PART_BYTES - 1) // config.SPLIT_PART_BYTES
        # Balanced parts: near-equal pieces instead of fixed-size
        # chunks, so a file barely over the cap becomes two even
        # halves instead of a 1.95 GB chunk plus a tiny tail.
        # ceil(size / count) is always <= SPLIT_PART_BYTES.
        part_size = (size + count - 1) // count
        note = (f"Splitting {os.path.basename(path)} "
                f"({human_bytes(size)}) into {count} parts…")
        if editor is not None:
            await editor.edit(note, force=True)
        elif status_msg is not None:
            try:
                await status_msg.edit_text(note)
            except Exception:
                pass
        parts = await run_blocking(_split_file_sync, path, part_size)
        out.extend(parts)
    return out
