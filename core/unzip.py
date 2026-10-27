"""Unzip support for the All-4-url bot.

Two call sites share this engine:
  * URL downloads that turn out to be a zip (plugins/uploader.py
    shows a "Unzip & send contents" / "Send zip as-is" prompt);
  * /unzip replying to a zip file the owner sent (plugins/unzip.py).

One-by-one extraction (v1.23.0, the owner's design):
  The old engine extracted EVERYTHING first, then uploaded —
  so disk had to hold zip + all extracted files at once, which
  is why total extracted was capped at 1 GB. The new engine
  extracts one file, the caller uploads it and deletes it,
  then the next file is extracted. Disk only ever holds
  zip + the largest single file inside it, so the total
  extracted can be far larger than free disk — it is now
  capped at 8 GB as a bandwidth/time guard (same as /sdl's
  total), not as a disk guard.

The key trick from before is kept: the total uncompressed
size, the largest file, and the file count are all known
BEFORE extracting anything. A zip's central directory lists
every entry's uncompressed size, so zip_info() sums those
sizes without reading a single byte of file data.

Safety, in order:
  * zip-slip: entries with absolute paths or ".." segments are
    skipped, never written outside the extract dir.
  * zip bombs / lying headers: extraction of one entry aborts
    that entry (not the whole job) the moment its actually
    written bytes pass its declared size + 1 KB — a lying
    header cannot expand past the limit, and the other files
    still go out.
  * symlinks: entries whose Unix mode says "symlink" are skipped
    (extracting one could point a later file outside the dir).
  * encrypted entries: skipped (no password prompt in v1 — the
    bot would have to ask, and the entry can't be verified
    without it). They are reported, not silently dropped.
  * nested zips: extracted as ordinary files, NOT recursed into
    (v1 scope — the owner can /unzip the inner zip afterwards).
  * duplicate basenames: Telegram has no folders, so extracted
    files are flattened to basenames, with _1, _2 suffixes when
    two entries share a name.
  * per-file Telegram cap: a single entry over Telegram's
    2 GB bot cap is skipped, not extracted — it could never
    be sent as one file, and extracting it would only waste
    disk and time.
  * disk fit, per file: before extracting each entry, free
    disk must exceed that entry's size + headroom. A file
    that does not fit is skipped, not extracted, and the
    other files still go out. This is checked per file, not
    once for the whole zip, because free disk changes while
    other jobs run alongside.
"""

import os
import shutil
import zipfile

import config
from core.utils import human_bytes

#: A zip file itself may not be bigger than this. 1 GB covers
#: the owner's 600 MB and 900 MB zips; a bigger zip plus its
#: largest file plus headroom would not fit in 2 GB of disk.
MAX_ZIP_BYTES = 1024 * 1024 * 1024
#: All extracted (and sent) files together may not exceed this.
#: This is now a bandwidth/time guard, NOT a disk guard —
#: one-by-one extraction never holds the total on disk.
#: 8 GB matches /sdl's total cap.
MAX_EXTRACT_BYTES = 8 * 1024 * 1024 * 1024
#: At most this many files out of one zip (each is a Telegram
#: upload of its own — more than this is a slog, not a feature).
#: This is now a time/bandwidth guard, not a disk guard.
MAX_EXTRACT_FILES = 50
#: Free disk that must remain after extracting one entry
#: (same margin the upage uploader reserves).
HEADROOM_BYTES = 256 * 1024 * 1024


class ZipTooBig(Exception):
    """Raised by the pre-check: message is safe to show the owner."""


class ZipBad(Exception):
    """Not a readable zip at all."""


def is_zip_file(path: str) -> bool:
    """True for a real zip (magic bytes), not just a .zip name."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
        return magic in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    except OSError:
        return False


def _is_dir_entry(info: zipfile.ZipInfo) -> bool:
    return info.filename.endswith("/") or info.filename.endswith("\\")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix file type lives in the top 16 bits of external_attr.
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _is_unsafe(info: zipfile.ZipInfo) -> bool:
    name = info.filename.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    return (not parts or any(p == ".." for p in parts)
            or name.startswith("/"))


def zip_info(path: str) -> dict:
    """Pre-check a zip WITHOUT extracting anything.

    Returns {"total": uncompressed bytes of sendable files,
    "count": sendable file entries, "files": [names],
    "encrypted": [names], "skipped": [names],
    "too_big": [names over Telegram's cap],
    "largest": largest sendable file size}.
    Raises ZipBad for unreadable/corrupt zips, ZipTooBig when
    the count or the sendable total breaks a cap — with a
    message the bot can show the owner verbatim.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ZipBad(f"not a readable zip file ({exc})")
    total = 0
    largest = 0
    files, encrypted, skipped, too_big = [], [], [], []
    for info in infos:
        if _is_dir_entry(info):
            continue
        if _is_symlink(info):
            skipped.append(info.filename)
            continue
        if _is_unsafe(info):
            skipped.append(info.filename)
            continue
        if info.flag_bits & 0x1:
            encrypted.append(info.filename)
            continue
        size = int(info.file_size or 0)
        if size == 0:
            skipped.append(info.filename)
            continue
        if size > config.MAX_FILE_SIZE:
            too_big.append(info.filename)
            continue
        total += size
        largest = max(largest, size)
        files.append(info.filename)
    if len(files) > MAX_EXTRACT_FILES:
        raise ZipTooBig(
            f"this zip has {len(files)} files — over the "
            f"{MAX_EXTRACT_FILES}-file limit, so I can't unzip it here")
    if total > MAX_EXTRACT_BYTES:
        raise ZipTooBig(
            f"this zip expands to {human_bytes(total)} — over the "
            f"{human_bytes(MAX_EXTRACT_BYTES)} total unzip limit, "
            f"so I can't unzip it here")
    return {"total": total, "count": len(files), "files": files,
            "encrypted": encrypted, "skipped": skipped,
            "too_big": too_big, "largest": largest}


def _free_disk_bytes(path: str) -> int | None:
    try:
        return shutil.disk_usage(os.path.dirname(path) or ".").free
    except Exception:
        return None


def check_disk_room(path: str, largest: int) -> None:
    """Raise ZipTooBig if even the largest file cannot be extracted.

    Called after zip_info() and before iter_extracted(), so the
    owner gets a plain message instead of a job that extracts
    nothing. This checks the LARGEST single file, not the total
    — one-by-one extraction never holds the total on disk.
    Individual files that do not fit later (disk filled by
    another job) are skipped by iter_extracted() itself.
    """
    free = _free_disk_bytes(path)
    if free is not None and largest + HEADROOM_BYTES > free:
        raise ZipTooBig(
            f"the largest file inside is {human_bytes(largest)}, "
            f"but the server only has {human_bytes(free)} free — "
            f"after extracting that file there would not be "
            f"enough disk left, so I can't unzip it here")


def iter_extracted(zip_path: str, dest_dir: str, skipped: list):
    """Yield extracted file paths, ONE AT A TIME.

    The caller uploads each yielded path and deletes it, then
    calls next() for the following file. Disk therefore holds
    at most one extracted file at a time (plus the zip itself).
    The generator also deletes the previously yielded path if
    the caller did not — so a caller that forgets to delete
    cannot silently fill the disk either.

    `skipped` is a list the generator appends to for every
    entry it does not yield (unsafe, symlink, encrypted,
    empty, over Telegram's cap, does-not-fit-on-disk, or a
    lying header). The caller reads it after iteration ends.
    A failure on one entry never aborts the whole job: that
    entry is deleted and skipped, and the next entry follows.
    """
    os.makedirs(dest_dir, exist_ok=True)
    seen: dict[str, int] = {}
    prev_path: str | None = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for entry in zf.infolist():
                # Clean up the previous file before extracting
                # the next one, in case the caller did not.
                if prev_path is not None:
                    try:
                        if os.path.exists(prev_path):
                            os.unlink(prev_path)
                    except OSError:
                        pass
                    prev_path = None
                if _is_dir_entry(entry):
                    continue
                if _is_symlink(entry):
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                if _is_unsafe(entry):
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                if entry.flag_bits & 0x1:
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                size = int(entry.file_size or 0)
                if size == 0:
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                if size > config.MAX_FILE_SIZE:
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                # Per-file disk fit, checked now, not once for
                # the whole zip — free disk changes while other
                # jobs run alongside.
                free = _free_disk_bytes(dest_dir)
                if free is not None and size + HEADROOM_BYTES > free:
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                name = entry.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                base = parts[-1]
                if base in seen:
                    seen[base] += 1
                    root, ext = os.path.splitext(base)
                    base = f"{root}_{seen[base]}{ext}"
                else:
                    seen[base] = 0
                dest = os.path.join(dest_dir, base)
                written = 0
                ok = True
                try:
                    with zf.open(entry) as src, open(dest, "wb") as fh:
                        while True:
                            chunk = src.read(256 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > size + 1024:
                                # Lying header: this entry alone
                                # expands past its declared size.
                                ok = False
                                break
                            fh.write(chunk)
                except Exception:
                    ok = False
                if not ok:
                    try:
                        os.unlink(dest)
                    except OSError:
                        pass
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                prev_path = dest
                yield dest
    finally:
        if prev_path is not None:
            try:
                if os.path.exists(prev_path):
                    os.unlink(prev_path)
            except OSError:
                pass
