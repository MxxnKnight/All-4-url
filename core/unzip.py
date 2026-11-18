"""Archive support (.zip/.rar/.7z) for the All-4-url bot.

Two call sites share this engine:
  * URL downloads that turn out to be an archive
    (plugins/uploader.py shows an "Unzip & send contents" /
    "Send as-is" prompt);
  * /unzip replying to an archive file the owner sent
    (plugins/unzip.py), and the file menu's Unzip button.

.zip files are read with Python's zipfile. .rar and .7z files
(v1.44.0) go through the `7z` command-line tool (p7zip-full in
the Dockerfile) — its `l -slt` listing for the pre-check and
`e` (extract, flattened) one entry at a time. Both formats
share the same caps, the same one-by-one contract, and the
same safety rules below.

One-by-one extraction (v1.23.0, the owner's design):
  The old engine extracted EVERYTHING first, then uploaded —
  so disk had to hold zip + all extracted files at once, which
  is why total extracted was capped at 1 GB. The new engine
  extracts one file, the caller uploads it and deletes it,
  then the next file is extracted. Disk only ever holds
  zip + the largest single file inside it, so the total
  extracted can be far larger than free disk — it is now
  capped at 20 GB as a bandwidth/time guard (same as the
  byte-split total), not as a disk guard.

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
    2 GB bot cap is NOT skipped anymore (v1.40.0): it is
    extracted like any other file, and the caller splits it
    into .001/.002 parts before sending (the byte-splitter
    already does this for every other oversize file, so a
    10 GB video inside a zip now arrives as mkv.001/.002
    instead of being silently dropped). The split needs
    2x the file on disk, which maybe_split checks itself.
  * disk fit, per file: before extracting each entry, free
    disk must exceed that entry's size + headroom. A file
    that does not fit is skipped, not extracted, and the
    other files still go out. This is checked per file, not
    once for the whole zip, because free disk changes while
    other jobs run alongside.
"""

import os
import shutil
import subprocess
import zipfile

from core.utils import human_bytes

#: A zip file itself may not be bigger than this. 20 GB matches
#: the extract total cap. A zip sent to the bot can never exceed
#: Telegram's own 2 GB file cap, and a zip downloaded from a
#: link over 2 GB is split into .001 parts before it ever
#: reaches the unzipper (plugins/uploader.py splits first,
#: prompts second) — so the only zips that reach this limit
#: are ones the bot assembled itself: /joinzip joins split
#: archives on disk, and a 10 GB movie zip is the whole point
#: of that command (v1.40.0 raised this from 2 GB for it).
MAX_ZIP_BYTES = 20 * 1024 * 1024 * 1024
#: All extracted (and sent) files together may not exceed this.
#: This is now a bandwidth/time guard, NOT a disk guard —
#: one-by-one extraction never holds the total on disk.
#: 20 GB matches the byte-split total cap.
MAX_EXTRACT_BYTES = 20 * 1024 * 1024 * 1024
#: At most this many files out of one zip. Count alone was a bad
#: rule on its own: a 10 MB zip with 65 tiny files is easy, a
#: 1 GB zip with 65 huge files is hard — so total (20 GB) and
#: per-file (2 GB) caps are the real limits, and count is only
#: a guard against thousands of separate Telegram messages in
#: one job (chat spam, FloodWait, job time), not a disk guard.
#: 1000 covers the owner's many-file zips with room to spare,
#: while still refusing absurd 1000+ file jobs that would flood
#: the chat for hours.
MAX_EXTRACT_FILES = 1000
#: Free disk that must remain after extracting one entry
#: (same margin the upage uploader reserves).
HEADROOM_BYTES = 256 * 1024 * 1024


class ZipTooBig(Exception):
    """Raised by the pre-check: message is safe to show the owner."""


class ZipBad(Exception):
    """Not a readable zip at all."""


#: Archive extensions the engine accepts (v1.44.0: rar/7z join zip).
ARCHIVE_EXTS = {".zip", ".rar", ".7z"}


def archive_kind(path: str) -> str | None:
    """'zip' | 'rar' | '7z' | None, from magic bytes (extension fallback)."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(8)
    except OSError:
        return None
    if magic[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if magic[:4] == b"Rar!":
        return "rar"
    if magic[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    ext = os.path.splitext(path)[1].lower()
    if ext in ARCHIVE_EXTS:
        return ext[1:]
    return None


def is_archive_file(path: str) -> bool:
    """True for a real .zip/.rar/.7z archive (magic bytes or name)."""
    return archive_kind(path) is not None


def is_zip_file(path: str) -> bool:
    """True for a real zip (magic bytes), not just a .zip name."""
    return archive_kind(path) == "zip"


def _is_dir_entry(info: zipfile.ZipInfo) -> bool:
    return info.filename.endswith("/") or info.filename.endswith("\\")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix file type lives in the top 16 bits of external_attr.
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _is_unsafe_name(name: str) -> bool:
    """True when an entry name would escape the extract dir."""
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    return (not parts or any(p == ".." for p in parts)
            or name.startswith("/"))


def _is_unsafe(info: zipfile.ZipInfo) -> bool:
    return _is_unsafe_name(info.filename)


def zip_info(path: str) -> dict:
    """Pre-check a zip WITHOUT extracting anything.

    Returns {"total": uncompressed bytes of sendable files,
    "count": sendable file entries, "files": [names],
    "entries": [(name, size)] in zip order (for listing the
    contents in the unzip prompt — v1.43.0),
    "encrypted": [names], "skipped": [names],
    "too_big": [] (kept for callers — oversize entries are
    sendable since v1.40.0, split into parts before sending),
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
    files, entries, encrypted, skipped, too_big = [], [], [], [], []
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
        # No per-file size exclusion: an entry over Telegram's
        # cap is extracted and split into .001/.002 parts by
        # the caller (v1.40.0) instead of being skipped.
        total += size
        largest = max(largest, size)
        files.append(info.filename)
        entries.append((info.filename, size))
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
            "entries": entries, "kind": "zip",
            "encrypted": encrypted, "skipped": skipped,
            "too_big": too_big, "largest": largest}


def _sevenz_list(path: str) -> list[dict]:
    """Entries of a .rar/.7z archive via `7z l -slt`.

    Returns [{name, size, encrypted, is_dir}]. Raises ZipBad when
    the 7z tool is missing or the archive can't be listed.
    """
    if shutil.which("7z") is None:
        raise ZipBad("the 7z tool isn't installed on this server, "
                     "so I can't read .rar/.7z files")
    try:
        proc = subprocess.run(
            ["7z", "l", "-slt", path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ZipBad(f"could not list that archive ({exc})")
    if proc.returncode != 0:
        raise ZipBad("not a readable archive (7z could not list it)")
    entries: list[dict] = []
    block: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            if "Path" in block:
                entries.append(_sevenz_block(block))
            block = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            block[key.strip()] = value.strip()
    if "Path" in block:
        entries.append(_sevenz_block(block))
    return entries


def _sevenz_block(block: dict[str, str]) -> dict:
    size = 0
    try:
        size = int(block.get("Size", "0") or 0)
    except ValueError:
        pass
    return {
        "name": block.get("Path", ""),
        "size": size,
        "encrypted": (block.get("Encrypted", "-").strip() == "+"),
        "is_dir": (block.get("Folder", "-").strip() == "+"),
    }


def _sevenz_info(path: str, kind: str) -> dict:
    """zip_info's shape for a .rar/.7z archive (no extraction)."""
    entries_in = _sevenz_list(path)
    total = 0
    largest = 0
    files, entries, encrypted, skipped = [], [], [], []
    for entry in entries_in:
        name = entry["name"]
        if entry["is_dir"]:
            continue
        if _is_unsafe_name(name):
            skipped.append(name)
            continue
        if entry["encrypted"]:
            encrypted.append(name)
            continue
        size = entry["size"]
        if size == 0:
            skipped.append(name)
            continue
        total += size
        largest = max(largest, size)
        files.append(name)
        entries.append((name, size))
    if len(files) > MAX_EXTRACT_FILES:
        raise ZipTooBig(
            f"this archive has {len(files)} files — over the "
            f"{MAX_EXTRACT_FILES}-file limit, so I can't unpack it here")
    if total > MAX_EXTRACT_BYTES:
        raise ZipTooBig(
            f"this archive expands to {human_bytes(total)} — over the "
            f"{human_bytes(MAX_EXTRACT_BYTES)} total unpack limit, "
            f"so I can't unpack it here")
    return {"total": total, "count": len(files), "files": files,
            "entries": entries, "kind": kind,
            "encrypted": encrypted, "skipped": skipped,
            "too_big": [], "largest": largest}


def archive_info(path: str) -> dict:
    """zip_info, for any supported archive (.zip/.rar/.7z).

    Same dict shape as zip_info, plus a "kind" key ("zip", "rar"
    or "7z"). Raises ZipBad for unreadable archives, ZipTooBig
    when the count or the sendable total breaks a cap.
    """
    kind = archive_kind(path)
    if kind is None:
        raise ZipBad("not a supported archive file (.zip/.rar/.7z)")
    if kind == "zip":
        return zip_info(path)
    return _sevenz_info(path, kind)


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


def iter_extracted(archive_path: str, dest_dir: str, skipped: list):
    """Yield extracted file paths, ONE AT A TIME.

    The caller uploads each yielded path and deletes it, then
    calls next() for the following file. Disk therefore holds
    at most one extracted file at a time (plus the archive
    itself). The generator also deletes the previously yielded
    path if the caller did not — so a caller that forgets to
    delete cannot silently fill the disk either.

    `skipped` is a list the generator appends to for every
    entry it does not yield (unsafe, symlink, encrypted,
    empty, over Telegram's cap, does-not-fit-on-disk, or a
    lying header). The caller reads it after iteration ends.
    A failure on one entry never aborts the whole job: that
    entry is deleted and skipped, and the next entry follows.

    Dispatches on the archive kind: .zip via zipfile, .rar/.7z
    via the 7z tool (v1.44.0).
    """
    kind = archive_kind(archive_path)
    if kind == "zip":
        yield from _iter_zip(archive_path, dest_dir, skipped)
    elif kind in ("rar", "7z"):
        yield from _iter_7z(archive_path, dest_dir, skipped, kind)
    else:
        return


def _extract_7z_entry(archive_path: str, entry: str, size: int,
                      tmp_dir: str, dest: str) -> bool:
    """Extract ONE .rar/.7z entry to `dest` via `7z e`.

    `7z e` flattens paths, so the entry lands in tmp_dir under
    its basename; it is moved to `dest` (the caller's deduped
    name) only when it is a real regular file whose size
    matches the listing. Symlinks, missing files, oversize
    writes (lying header) and 7z failures all return False.
    """
    for leftover in os.listdir(tmp_dir):
        try:
            os.unlink(os.path.join(tmp_dir, leftover))
        except OSError:
            pass
    try:
        proc = subprocess.run(
            ["7z", "e", "-y", f"-o{tmp_dir}", archive_path, entry],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    base = entry.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    got = os.path.join(tmp_dir, base)
    try:
        if not os.path.isfile(got) or os.path.islink(got):
            return False
        if os.path.getsize(got) > size + 1024:
            return False
    except OSError:
        return False
    try:
        shutil.move(got, dest)
    except OSError:
        return False
    return True


def _iter_7z(archive_path: str, dest_dir: str, skipped: list,
             kind: str):
    """One-by-one extraction for .rar/.7z (see iter_extracted)."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        info = _sevenz_info(archive_path, kind)
    except (ZipBad, ZipTooBig):
        return
    tmp_dir = os.path.join(dest_dir, ".7z_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    seen: dict[str, int] = {}
    prev_path: str | None = None
    try:
        for name, size in info["entries"]:
            if prev_path is not None:
                try:
                    if os.path.exists(prev_path):
                        os.unlink(prev_path)
                except OSError:
                    pass
                prev_path = None
            if name.startswith("-"):
                # 7z would read a leading dash as a switch.
                if name not in skipped:
                    skipped.append(name)
                continue
            free = _free_disk_bytes(dest_dir)
            if free is not None and size + HEADROOM_BYTES > free:
                if name not in skipped:
                    skipped.append(name)
                continue
            base = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if base in seen:
                seen[base] += 1
                root, ext = os.path.splitext(base)
                base = f"{root}_{seen[base]}{ext}"
            else:
                seen[base] = 0
            dest = os.path.join(dest_dir, base)
            if not _extract_7z_entry(archive_path, name, size,
                                     tmp_dir, dest):
                try:
                    if os.path.exists(dest):
                        os.unlink(dest)
                except OSError:
                    pass
                if name not in skipped:
                    skipped.append(name)
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
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _iter_zip(zip_path: str, dest_dir: str, skipped: list):
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
                # No per-file size exclusion: an entry over
                # Telegram's cap is extracted and split into
                # .001/.002 parts by the caller (v1.40.0).
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
