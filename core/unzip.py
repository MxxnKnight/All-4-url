"""Unzip support for the All-4-url bot.

Two call sites share this engine:
  * URL downloads that turn out to be a zip (plugins/uploader.py
    shows a "Unzip & send contents" / "Send zip as-is" prompt);
  * /unzip replying to a zip file the owner sent (plugins/unzip.py).

Limits are the whole point on a free host (512 MB RAM, 0.1 vCPU,
2 GB ephemeral disk): the zip and its contents both live on disk
at once, and every extracted file is then uploaded to Telegram
(counting toward monthly outbound bandwidth). So nothing here
extracts first and asks questions later.

The key trick the owner asked for: the total uncompressed size is
known BEFORE extracting anything. A zip's central directory lists
every entry's uncompressed size, so zip_info() sums those sizes
without reading a single byte of file data. If that sum (or the
file count, or the free disk) is over the cap, the caller can
tell the owner "this zip exceeds the limit" and never start
extracting — exactly the message the owner wanted.

Safety, in order:
  * zip-slip: entries with absolute paths or ".." segments are
    skipped, never written outside the extract dir.
  * zip bombs: the header total is capped, AND extraction itself
    aborts the moment the bytes actually written pass the cap —
    a lying header cannot expand past the limit.
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
"""

import os
import zipfile

from core.utils import human_bytes

#: A zip file itself may not be bigger than this (Telegram's own
#: bot cap is 2 GB; the free host's disk is the tighter bound).
MAX_ZIP_BYTES = 500 * 1024 * 1024
#: All extracted files together may not exceed this.
MAX_EXTRACT_BYTES = 1000 * 1024 * 1024
#: At most this many files out of one zip (each is a Telegram
#: upload of its own — more than this is a slog, not a feature).
MAX_EXTRACT_FILES = 50
#: Free disk that must remain after extraction (same margin the
#: upage uploader reserves).
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


def zip_info(path: str) -> dict:
    """Pre-check a zip WITHOUT extracting anything.

    Returns {"total": uncompressed bytes, "count": file entries,
    "files": [names], "encrypted": [names], "skipped": [names]}.
    Raises ZipBad for unreadable/corrupt zips, ZipTooBig when the
    header totals already break a cap — with a message the bot
    can show the owner verbatim.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ZipBad(f"not a readable zip file ({exc})")
    total = 0
    files, encrypted, skipped = [], [], []
    for info in infos:
        if _is_dir_entry(info):
            continue
        if _is_symlink(info):
            skipped.append(info.filename)
            continue
        name = info.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts) \
                or name.startswith("/"):
            skipped.append(info.filename)
            continue
        if info.flag_bits & 0x1:
            encrypted.append(info.filename)
            continue
        total += int(info.file_size or 0)
        files.append(info.filename)
    if len(files) > MAX_EXTRACT_FILES:
        raise ZipTooBig(
            f"this zip has {len(files)} files — over the "
            f"{MAX_EXTRACT_FILES}-file limit, so I can't unzip it here")
    if total > MAX_EXTRACT_BYTES:
        raise ZipTooBig(
            f"this zip expands to {human_bytes(total)} — over the "
            f"{human_bytes(MAX_EXTRACT_BYTES)} unzip limit, so I "
            f"can't unzip it on this server (its disk is only 2 GB)")
    return {"total": total, "count": len(files), "files": files,
            "encrypted": encrypted, "skipped": skipped}


def _free_disk_bytes(path: str) -> int | None:
    try:
        import shutil
        return shutil.disk_usage(os.path.dirname(path) or ".").free
    except Exception:
        return None


def check_disk_room(path: str, total: int) -> None:
    """Raise ZipTooBig if extraction would leave too little disk.

    Called after zip_info() and before extract_zip(), so the
    owner gets the "exceeds the disk" message instead of a
    half-extracted mess.
    """
    free = _free_disk_bytes(path)
    if free is not None and total + HEADROOM_BYTES > free:
        raise ZipTooBig(
            f"this zip expands to {human_bytes(total)}, but the "
            f"server only has {human_bytes(free)} free — after "
            f"unzip there would not be enough disk left, so I "
            f"can't unzip it here")


def extract_zip(path: str, dest_dir: str) -> tuple[list[str], list[str]]:
    """Extract a pre-checked zip safely. Returns (paths, skipped).

    Skipped entries (unsafe paths, symlinks, encrypted files) are
    reported by name, never silently dropped. Any failure partway
    through deletes what was extracted, so a half-unzipped folder
    can never be sent as if it were complete.
    """
    info = zip_info(path)
    check_disk_room(path, info["total"])
    os.makedirs(dest_dir, exist_ok=True)
    extracted: list[str] = []
    skipped = list(info["skipped"]) + info["encrypted"]
    seen: dict[str, int] = {}
    written_total = 0
    try:
        with zipfile.ZipFile(path) as zf:
            for entry in zf.infolist():
                if entry.filename in skipped or _is_dir_entry(entry):
                    continue
                if _is_symlink(entry) or (entry.flag_bits & 0x1):
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                name = entry.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if not parts or any(p == ".." for p in parts) \
                        or name.startswith("/"):
                    if entry.filename not in skipped:
                        skipped.append(entry.filename)
                    continue
                base = parts[-1]
                if base in seen:
                    seen[base] += 1
                    root, ext = os.path.splitext(base)
                    base = f"{root}_{seen[base]}{ext}"
                else:
                    seen[base] = 0
                dest = os.path.join(dest_dir, base)
                written = 0
                with zf.open(entry) as src, open(dest, "wb") as fh:
                    while True:
                        chunk = src.read(256 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        written_total += len(chunk)
                        if written_total > MAX_EXTRACT_BYTES \
                                or written > int(entry.file_size or 0) \
                                + 1024:
                            raise ZipTooBig(
                                "this zip expanded past the limit "
                                "while unzipping — stopping here")
                        fh.write(chunk)
                extracted.append(dest)
    except Exception:
        for p in extracted:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise
    return extracted, skipped
