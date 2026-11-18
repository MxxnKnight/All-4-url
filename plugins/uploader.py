"""URL uploader — the resolver chain.

For any non-Telegram URL the owner sends, engines try in this order:

  Terabox domain      -> custom Terabox extractor (core/extractors/terabox)
  Instagram /p/ post  -> yt-dlp metadata pass (carousel images)
                         -> gallery-dl -> yt-dlp -> scrape
  Instagram /reel/    -> yt-dlp -> gallery-dl -> scrape
  Direct file URL     -> streamed straight down (core/extractors/direct)
  Everything else     -> yt-dlp (with format buttons when it finds
                         several resolutions) -> gallery-dl -> scrape
  Oversize downloads  -> split into ~1.95 GB .001/.002 parts
                         (core/splitter) instead of being refused
  Scrape last resort  -> static HTML, then a data: playlist, then a
                         real headless browser (browser_scan)

Only one engine's success is needed; failures cascade to the next, and
the final error message tells the owner what each engine said.
"""

import asyncio
import os
import re
import shutil
import time
from urllib.parse import unquote, urlparse

from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from core.extractors import (browser_scan, gallery_engine, scrape,
                             site_registry, terabox)
from core import jobs, splitter, stats, unzip, upage
from core.filename_parser import clean_display
from core.extractors.direct import (check_direct, download_file,
                                    is_playlist_url, safe_filename)
from core.extractors.player_scan import FallThrough, HostError
from core.extractors.ytdlp_engine import (YtdlpError, best_formats,
                                          carousel_image_urls, download,
                                          extract_info)
from core.jobs import JobCancelled
from core.sender import (send_files, send_files_routed,
                         send_files_split)
from core.utils import (JOB_SEMAPHORE, SizeBlocked, check_size, cleanup_dir,
                        human_bytes, is_admin_id, is_owner_id, list_files,
                        make_job_dir, admin_only, progress_text, react,
                        react_to, run_blocking)

URL_RE = re.compile(r"https?://[^\s]+")

# Jobs waiting on a format-button tap: job_id -> job dict
PENDING: dict[str, dict] = {}
# Zip jobs waiting on an unzip-button tap: job_id -> job dict.
# Unlike the format picker (which has no TTL — a known wart), these
# DO expire: a job_dir left waiting forever would pin disk space.
PENDING_UNZIP: dict[str, dict] = {}
UNZIP_TTL = 3600
_counter = 0


def _prune_unzip_pending() -> None:
    now = int(time.time())
    for job_id, job in list(PENDING_UNZIP.items()):
        if now - job.get("ts", 0) > UNZIP_TTL:
            cleanup_dir(job["job_dir"])
            PENDING_UNZIP.pop(job_id, None)


def _unzip_prompt_text(job: dict) -> str:
    """The prompt body for an unpack job (re-rendered on every
    toggle tap, so the ticks always match the selection)."""
    entries = job["entries"]
    selected = job["selected"]
    listed = []
    for idx, (name, size) in enumerate(entries[:_MAX_LISTED_FILES]):
        shown = clean_display(name) or name
        shown = shown if len(shown) <= 55 else "…" + shown[-54:]
        tick = "✅" if idx in selected else "⬜"
        listed.append(f"{tick} {shown} ({human_bytes(size)})")
    if len(entries) > _MAX_LISTED_FILES:
        listed.append(f"• …and {len(entries) - _MAX_LISTED_FILES} more")
    listing = ("\n" + "\n".join(listed)) if listed else ""
    return (
        f"📦 **{job['header']}**\n"
        f"{job['kind_label']}: {human_bytes(job['size'])} · "
        f"unpacks to {human_bytes(job['total'])} · "
        f"{job['count']} file(s)"
        + listing
        + (f"\n⚠️ {len(job['encrypted'])} password-protected "
           f"file(s) will be skipped" if job["encrypted"] else "")
        + "\n\nTap a file to tick or untick it — Unpack sends only "
          "the ticked files. Leave everything ticked to unpack it all.")


def _unzip_prompt_markup(job: dict) -> InlineKeyboardMarkup:
    """Toggle buttons for the listed files + the actions."""
    rows = []
    for idx, (name, _size) in enumerate(
            job["entries"][:_MAX_LISTED_FILES]):
        shown = clean_display(name) or name
        shown = shown if len(shown) <= 24 else shown[:23] + "…"
        tick = "✅" if idx in job["selected"] else "⬜"
        rows.append([InlineKeyboardButton(
            f"{tick} {shown}",
            callback_data=f"unzip:{job['id']}:t:{idx}")])
    n = len(job["selected"])
    rows.append([
        InlineKeyboardButton(f"📂 Unpack selected ({n})",
                             callback_data=f"unzip:{job['id']}:go"),
        InlineKeyboardButton("📦 Send archive as-is",
                             callback_data=f"unzip:{job['id']}:asis"),
    ])
    rows.append([
        InlineKeyboardButton("✅ All",
                             callback_data=f"unzip:{job['id']}:all"),
        InlineKeyboardButton("⬜ None",
                             callback_data=f"unzip:{job['id']}:none"),
    ])
    return InlineKeyboardMarkup(rows)


async def _maybe_unzip_prompt(client: Client, chat_id: int, paths: list[str],
                              caption: str, job_dir: str) -> bool:
    """Intercept a single downloaded archive with an unpack prompt.

    Returns True when the prompt is on screen — the caller must
    then NOT send the file and NOT clean up job_dir; the callback
    owns both from here. Returns False when the file is not an
    archive (or the prompt could not be shown), and the caller
    sends it normally. An archive that fails the pre-check is NOT
    prompted: the owner gets a persistent "exceeds" message and
    the archive itself is sent as-is.

    The prompt is its OWN message (v1.45.0): the shared progress
    message belongs to every running job, so a one-job question
    can't take it over. The prompt lists the contents with
    tap-to-tick buttons (v1.45.0) — the owner picks which files to
    unpack instead of all-or-nothing.
    """
    if len(paths) != 1 or not unzip.is_archive_file(paths[0]):
        return False
    zip_path = paths[0]
    kind = unzip.archive_kind(zip_path) or "zip"
    kind_label = {"zip": "Zip", "rar": "Rar", "7z": "7z"}.get(kind, "Archive")
    try:
        if os.path.getsize(zip_path) > unzip.MAX_ZIP_BYTES:
            raise unzip.ZipTooBig(
                f"this archive is {human_bytes(os.path.getsize(zip_path))} — "
                f"over the {human_bytes(unzip.MAX_ZIP_BYTES)} unpack "
                f"limit, so I can't unpack it here")
        info = unzip.archive_info(zip_path)
        unzip.check_disk_room(zip_path, info["largest"])
    except unzip.ZipTooBig as exc:
        # Persistent message (status is deleted after a normal send).
        try:
            await client.send_message(
                chat_id,
                f"📦 This archive is a {kind}, but I can't unpack it: {exc}\n"
                f"Sending the archive itself as-is instead.")
        except Exception:
            pass
        return False
    except unzip.ZipBad:
        return False
    except Exception:
        return False
    _prune_unzip_pending()
    job_id = _next_job_id()
    entries = info.get("entries") or [(name, 0)
                                      for name in info["files"]]
    # Everything listed starts ticked: tapping Unpack without
    # touching anything unpacks it all, exactly like before.
    job = {"id": job_id, "job_dir": job_dir, "path": zip_path,
           "chat_id": chat_id, "caption": caption,
           "ts": int(time.time()), "entries": entries,
           "selected": set(range(min(len(entries), _MAX_LISTED_FILES))),
           "touched": False,
           "header": clean_display(os.path.basename(zip_path))
           or os.path.basename(zip_path),
           "kind_label": kind_label, "size": os.path.getsize(zip_path),
           "total": info["total"], "count": info["count"],
           "encrypted": info["encrypted"]}
    PENDING_UNZIP[job_id] = job
    try:
        await client.send_message(chat_id, _unzip_prompt_text(job),
                                  reply_markup=_unzip_prompt_markup(job))
    except Exception:
        PENDING_UNZIP.pop(job_id, None)
        return False
    return True


#: How many archive entries the unzip prompt lists before "…and N more".
_MAX_LISTED_FILES = 10


async def _prompt_or_split(client, chat_id, paths, caption,
                           job_dir, job=None):
    """Ask to unpack a single archive BEFORE splitting it (v1.43.0).

    Returns (prompted, send_paths). A single archive within the
    unpack caps gets the prompt and the callback owns the job
    (prompted True, no paths) — so a season zip over Telegram's
    2 GB cap is unpacked and its episodes sent one by one instead
    of arriving as .zip.001/.002 parts nobody asked for. Anything
    else is split as usual and returned for sending — including
    an archive over the unpack caps, which the prompt already
    refused with a "can't unpack" message.
    `job`, when given, is the split progress target (see core/jobs).
    """
    if await _maybe_unzip_prompt(client, chat_id, paths, caption,
                                 job_dir):
        return True, []
    return False, await splitter.maybe_split(
        paths, editor=job, status_msg=None)


def _next_job_id() -> str:
    global _counter
    _counter += 1
    return f"{int(time.time()) % 100000}{_counter}"


# How many links one message may carry: each is a full download +
# upload job, so a paste of dozens would hold the handler for hours.
# The rest are skipped with a note (v1.38.0).
MAX_MULTI_LINKS = 10


def _extract_urls(message: Message) -> list[str]:
    """Every uploadable URL in the message, in order, de-duplicated.

    Telegram links are skipped — the restricted plugin owns those.
    """
    urls: list[str] = []
    for match in URL_RE.finditer(message.text or ""):
        url = match.group(0).rstrip(").,>]\"'")
        if "t.me/" in url or "telegram.me/" in url:
            continue
        if url not in urls:
            urls.append(url)
    return urls


def _extract_url(message: Message) -> str | None:
    """First uploadable URL in the message, or None."""
    urls = _extract_urls(message)
    return urls[0] if urls else None


# ------------------------------------------------------- join split archives
# (/joinzip, v1.40.0)

#: Split-archive part files look like `movie.zip.001` — a stem plus
#: a 3-digit part number. Only this shape is joined (7-Zip style,
#: the same shape the splitter produces).
_PART_RE = re.compile(r"^(.+)\.(\d{3})$")
#: At most this many parts in one /joinzip message — each is a
#: full download, and the 20 GB joined-size cap bounds the bytes.
MAX_JOIN_PARTS = 20


def _split_archive_parts(urls: list[str]):
    """Split `urls` into a split-archive part set, or say why not.

    Returns ("ok", stem, parts) where parts is a list of
    (part_number, url, filename) sorted by part number;
    ("missing", stem, missing_numbers) when the links are parts
    of one archive with gaps; or None when the message is not a
    part set at all (fewer than two part links, mixed stems,
    duplicate numbers, a part 000).
    """
    parsed = []
    for url in urls:
        name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        match = _PART_RE.match(name)
        if not match:
            return None
        parsed.append((int(match.group(2)), url, name, match.group(1)))
    if len(parsed) < 2:
        return None
    stems = {p[3] for p in parsed}
    if len(stems) != 1:
        return None
    numbers = [p[0] for p in parsed]
    if len(set(numbers)) != len(numbers):
        return None  # same part twice from different links: ambiguous
    if min(numbers) < 1:
        return None  # 7-Zip parts start at 001, never 000
    numbers.sort()
    if numbers != list(range(1, len(numbers) + 1)):
        missing = [n for n in range(1, numbers[-1] + 1)
                   if n not in numbers]
        return ("missing", parsed[0][3], missing)
    parts = sorted(parsed, key=lambda p: p[0])
    return ("ok", parsed[0][3],
            [(num, url, name) for num, url, name, _ in parts])


def _join_parts_sync(part_paths: list[str], dest_path: str) -> int:
    """Concatenate `part_paths` into `dest_path`; returns its size.

    Blocking (run through run_blocking). The parts are deleted
    only after the joined size matches the parts' sizes exactly —
    a short read deletes the partial join instead and raises.
    """
    expected = sum(os.path.getsize(p) for p in part_paths)
    try:
        with open(dest_path, "wb") as dst:
            for part in part_paths:
                with open(part, "rb") as src:
                    while True:
                        chunk = src.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
        size = os.path.getsize(dest_path)
        if size != expected:
            raise IOError(
                f"joined file is {human_bytes(size)}, not "
                f"{human_bytes(expected)} — refusing to use it")
    except Exception:
        try:
            os.unlink(dest_path)
        except OSError:
            pass
        raise
    for part in part_paths:
        try:
            os.unlink(part)
        except OSError:
            pass
    return size


def _check_join_room(total: int) -> None:
    """Raise SizeBlocked unless parts + joined file fit on disk.

    The join briefly holds both: the downloaded parts AND the
    joined file, so 2x the total must be free (plus headroom).
    """
    try:
        free = shutil.disk_usage(config.BASE_DIR).free
    except Exception:
        return  # can't measure; let the join try
    reserved = 0
    try:
        reserved = upage.pending_bytes()
    except Exception:
        reserved = 0
    if 2 * total + 256 * 1024 * 1024 + reserved > free:
        raise SizeBlocked(
            f"Joining {human_bytes(total)} needs "
            f"{human_bytes(2 * total)} free on the server (parts "
            f"plus the joined file) and only {human_bytes(free)} "
            f"is free right now — try again when the server is "
            f"less busy.")


_JOINZIP_USAGE = (
    "Usage: `/joinzip <link1> <link2> …` — send ALL the links of "
    "one split archive in one message (like `movie.zip.001`, "
    "`movie.zip.002`, …). I'll download the parts, join them, "
    "and if it's a zip, offer to unzip it; a big video inside "
    "arrives split into .mkv.001/.002 parts.")


@Client.on_message(filters.command("joinzip") & filters.private
                   & admin_only)
async def joinzip_cmd(client: Client, message: Message):
    """Join split-archive links, then unzip / split-and-send.

    The owner sends every part link of one archive in one
    message. Parts download one after another, join on disk,
    and a joined zip gets the usual unzip prompt — whose
    callback now splits oversize videos into .001/.002 parts
    instead of skipping them (v1.40.0). A joined non-zip
    (e.g. movie.mkv parts) is split and sent directly.
    """
    urls = _extract_urls(message)
    if not urls:
        await message.reply_text(_JOINZIP_USAGE)
        return
    parsed = _split_archive_parts(urls)
    if parsed is None:
        await message.reply_text(
            "I need the links of ONE split archive — every link "
            "ending in .001, .002, … with the same file name "
            "before the number.\n\n" + _JOINZIP_USAGE)
        return
    if parsed[0] == "missing":
        _, stem, missing = parsed
        nums = ", ".join(f".{n:03d}" for n in missing)
        await message.reply_text(
            f"That set is missing part(s) {nums} of {stem} — "
            f"a zip with a hole in the middle can't be opened. "
            f"Send ALL the parts in one message.")
        return
    _, stem, parts = parsed
    if len(parts) > MAX_JOIN_PARTS:
        await message.reply_text(
            f"That's {len(parts)} parts — I take at most "
            f"{MAX_JOIN_PARTS} in one command.")
        return
    if stats.bandwidth_blocked():
        await message.reply_text(stats.bandwidth_pause_text())
        return
    await react(message, "👀")
    user_id = message.from_user.id if message.from_user else None
    job = await jobs.register(client, message.chat.id,
                              f"Join: {stem}", reply_to=message)
    job_dir = make_job_dir()

    async def _fail(text: str) -> None:
        await job.fail(text)
        await react(message, "👎")
        stats.record_job("uploader", ok=False)

    try:
        async with JOB_SEMAPHORE:
            # Probe every part first: all must be direct links,
            # and the total must fit the join + unzip caps.
            total = 0
            known = True
            for num, url, _name in parts:
                job.check_cancelled()
                info = await check_direct(url)
                if info is None:
                    await _fail(
                        f"❌ Part .{num:03d} isn't a direct "
                        f"download link — /joinzip needs direct "
                        f"links to the part files.")
                    return
                if info.get("size"):
                    total += info["size"]
                else:
                    known = False
            if known and total > unzip.MAX_ZIP_BYTES:
                await _fail(
                    f"❌ Those parts add up to {human_bytes(total)} "
                    f"— over the {human_bytes(unzip.MAX_ZIP_BYTES)} "
                    f"limit, so I can't join them here.")
                return
            if known:
                try:
                    _check_join_room(total)
                except SizeBlocked as exc:
                    await _fail(f"❌ {exc}")
                    return
            is_big = (not known) or total > config.MAX_FILE_SIZE
            async with splitter.big_file_guard(is_big):
                # Download the parts in order.
                part_paths = []
                for i, (num, url, name) in enumerate(parts, 1):
                    started = time.time()
                    label = (f"Downloading part {i} of {len(parts)} "
                             f"(.{num:03d})…")

                    async def _prog(current, total_b, _label=label,
                                    _started=started):
                        job.check_cancelled()
                        await job.edit(progress_text(
                            _label, current, total_b, _started))

                    part_paths.append(await download_file(
                        url, job_dir, progress_cb=_prog,
                        filename=safe_filename(
                            name, f"part.{num:03d}")))
                await job.edit("Joining the parts…", force=True)
                joined_path = os.path.join(
                    job_dir, safe_filename(stem, "joined"))
                await run_blocking(_join_parts_sync, part_paths,
                                   joined_path)
                # A joined zip gets the unzip prompt (the callback
                # owns the job from here); anything else is split
                # and sent like any other big download.
                caption = (f"Joined from {len(parts)} parts: {stem}")
                prompted = await _maybe_unzip_prompt(
                    client, message.chat.id, [joined_path], caption,
                    job_dir)
                if prompted:
                    job_dir = None  # the callback owns the dir now
                    return
                paths = await splitter.maybe_split([joined_path],
                                                   job, None)
            await send_files_routed(client, message.chat.id, paths,
                                    "uploader", caption=caption,
                                    job=job, user_id=user_id)
            await react(message, "👍")
            stats.record_job("uploader", ok=True)
    except JobCancelled:
        # Stopped by the owner: no 👎, no failed-job stat — a stop
        # is a choice, not a failure.
        await job.fail("⏹ Cancelled.")
    except SizeBlocked as exc:
        await _fail(f"❌ {exc}")
    except Exception as exc:
        await _fail(f"❌ Join failed: {exc}")
    finally:
        await job.finish()
        if job_dir is not None:
            cleanup_dir(job_dir)


# ---------------------------------------------------------------- Terabox UI

async def _terabox_flow(client: Client, message: Message, url: str,
                        job) -> bool:
    """Returns True when a file picker was shown (job not finished yet)."""
    files = await terabox.list_files(url)
    if len(files) == 1:
        check_size(files[0].get("size") or 0, files[0].get("name") or "file",
                   allow_split=True)
        return await _terabox_download(
            client, message, url, 0, files[0], job,
            big_file=(files[0].get("size") or 0) > config.MAX_FILE_SIZE,
            user_id=message.from_user.id if message.from_user else None)
    job_id = _next_job_id()
    PENDING[job_id] = {"type": "terabox", "url": url, "chat_id": message.chat.id,
                       "origin_chat": message.chat.id, "origin_msg": message.id}
    buttons = [
        [InlineKeyboardButton(
            f"{f['name'][:40]} · {human_bytes(f['size'])}",
            callback_data=f"tera:{job_id}:{i}")]
        for i, f in enumerate(files[:10])
    ]
    via_note = (" (via FileSnag — its links expire in a few hours, "
                "so pick promptly)" if files[0].get("via") == "filesnag"
                else "")
    # The picker is its OWN message (v1.45.0): the shared progress
    # message belongs to every running job, so a one-job question
    # can't take it over. The download registers a fresh job when
    # the owner picks.
    await client.send_message(
        message.chat.id,
        f"This Terabox share has several files — pick one:{via_note}",
        reply_markup=InlineKeyboardMarkup(buttons))
    return True


async def _terabox_download(client, message, url, index, info, job,
                          big_file=False, user_id=None):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        via = (info or {}).get("via")
        progress_label = ("Downloading from Terabox (via FileSnag)…"
                          if via == "filesnag"
                          else "Downloading from Terabox…")

        async def progress(done, total):
            job.check_cancelled()
            await job.edit(progress_text(progress_label,
                                         done, total, started))
        prompted = False
        try:
            async with splitter.big_file_guard(big_file):
                path = await terabox.download(url, job_dir, index=index,
                                              progress_cb=progress)
                prompted, paths = await _prompt_or_split(
                    client, message.chat.id, [path],
                    f"Terabox · {info['name'] if info else ''}",
                    job_dir, job=job)
            if not prompted:
                await job.edit("Uploading…", force=True)
                await send_files_routed(
                    client, message.chat.id, paths, "uploader",
                    caption=f"Terabox · {info['name'] if info else ''}",
                    job=job, user_id=user_id)
        finally:
            if not prompted:
                cleanup_dir(job_dir)
        return prompted


# ---------------------------------------------------------------- custom sites

async def _custom_flow(client: Client, message: Message, url: str,
                       job, site) -> bool:
    """A registered custom-site extractor (see site_registry).

    Returns True when an unzip prompt is on screen (job pending),
    else False. Resolves once for a size check, then downloads —
    the download resolves again so the media URL it uses is
    fresh (HLS tokens are short-lived; reusing an older one
    would fail for the same reason a 1DM link fails).
    """
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        user_id = message.from_user.id if message.from_user else None

        async def progress(done, total):
            job.check_cancelled()
            await job.edit(progress_text("Downloading…", done, total, started))

        prompted = False
        try:
            try:
                info = await site.resolve(url)
            except (FallThrough, SizeBlocked):
                raise
            except Exception as exc:
                # The URL belongs to this site, but the site's own
                # resolver could not produce media (template change,
                # dead page, Cloudflare, a moved domain). Hand the
                # link to the rest of the chain — yt-dlp, gallery-dl,
                # scrape and the browser — instead of failing a link
                # the browser could still play. Only the resolve step
                # falls through; a failure mid-download still fails
                # here (nothing has been written to disk yet).
                raise FallThrough(
                    f"the site's own resolver could not read it: {exc}") \
                    from exc
            if info.get("approx_size"):
                check_size(info["approx_size"], info.get("title") or "file",
                           allow_split=True)
            big = (info.get("approx_size") or 0) > config.MAX_FILE_SIZE
            async with splitter.big_file_guard(big):
                paths = await site.download(
                    url, job_dir, progress_cb=progress,
                    cancel_check=lambda: job.cancelled)
                prompted, paths = await _prompt_or_split(
                    client, message.chat.id, paths,
                    info.get("title", "")[:200], job_dir, job=job)
            if not prompted:
                await job.edit("Uploading…", force=True)
                await send_files_routed(client, message.chat.id, paths,
                                        "uploader",
                                        caption=info.get("title", "")[:200],
                                        job=job, user_id=user_id)
        finally:
            if not prompted:
                cleanup_dir(job_dir)
        return prompted


# ---------------------------------------------------------------- yt-dlp flow

async def _ytdlp_flow(client: Client, message: Message, url: str,
                      job, ask_format: bool = True) -> bool:
    """extract_info first; ask which format when there is a real choice.

    Returns True when a format picker was shown (job not finished yet)."""
    info = await extract_info(url)
    title = info.get("title") or "Download"
    formats = best_formats(info) if info.get("formats") else []
    heights = {f["height"] for f in formats}

    if ask_format and len(heights) >= 2:
        job_id = _next_job_id()
        PENDING[job_id] = {"type": "ytdlp", "url": url,
                           "chat_id": message.chat.id, "title": title,
                           "formats": formats,
                           "origin_chat": message.chat.id,
                           "origin_msg": message.id}
        buttons = [[InlineKeyboardButton(
            "⭐ Best quality", callback_data=f"fmt:{job_id}:best")]]
        for fmt in formats[:5]:
            size = f" · {human_bytes(fmt['filesize'])}" if fmt["filesize"] else ""
            buttons.append([InlineKeyboardButton(
                f"{fmt['height']}p{size}",
                callback_data=f"fmt:{job_id}:{fmt['format_id']}")])
        buttons.append([InlineKeyboardButton(
            "🎧 Audio only (mp3)", callback_data=f"fmt:{job_id}:audio")])
        # The picker is its OWN message (v1.45.0): the shared
        # progress message belongs to every running job, so a
        # one-job question can't take it over. The download
        # registers a fresh job when the owner picks.
        await client.send_message(
            message.chat.id,
            f"**{title[:120]}**\nPick a quality:",
            reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if formats:
        # No picker (reels / single format): still refuse a giant file
        # before spending the download on it — unless it fits the
        # splitter, in which case it goes out in parts.
        check_size(formats[0].get("filesize") or 0, title, allow_split=True)
    return await _ytdlp_download(
        client, message, url, title, job,
        big_file=bool(formats)
        and (formats[0].get("filesize") or 0) > config.MAX_FILE_SIZE,
        user_id=message.from_user.id if message.from_user else None)


async def _ytdlp_download(client, message, url, title, job,
                          format_id=None, audio_only=False, big_file=False,
                          user_id=None):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        async def progress(done, total):
            job.check_cancelled()
            await job.edit(progress_text("Downloading…", done, total, started))

        prompted = False
        try:
            async with splitter.big_file_guard(big_file):
                paths = await download(url, job_dir, format_id=format_id,
                                       audio_only=audio_only,
                                       progress_cb=progress,
                                       cancel_check=lambda: job.cancelled)
                prompted, paths = await _prompt_or_split(
                    client, message.chat.id, paths, title[:200],
                    job_dir, job=job)
            if not prompted:
                await job.edit("Uploading…", force=True)
                await send_files_routed(client, message.chat.id, paths,
                                        "uploader", caption=title[:200],
                                        job=job, user_id=user_id)
        finally:
            if not prompted:
                cleanup_dir(job_dir)
        return prompted


# ---------------------------------------------------------------- fallbacks

async def _gallery_flow(client, message, url, job):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        prompted = False
        try:
            await job.edit("Fetching with gallery-dl…")
            paths = await gallery_engine.download(
                url, job_dir, cancel_check=lambda: job.cancelled)
            # gallery-dl never tells us the size up front, so a big
            # file only shows itself here — maybe_split's own disk
            # check is what keeps the split safe.
            prompted, paths = await _prompt_or_split(
                client, message.chat.id, paths, url, job_dir, job=job)
            if not prompted:
                await send_files_routed(
                    client, message.chat.id, paths, "uploader",
                    caption=url, job=job,
                    user_id=message.from_user.id
                    if message.from_user else None)
        finally:
            if not prompted:
                cleanup_dir(job_dir)
        return prompted


async def _direct_flow(client, message, url, info, job):
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        async def progress(done, total):
            job.check_cancelled()
            await job.edit(progress_text("Downloading…", done, total, started))

        prompted = False
        try:
            big = (info.get("size") or 0) > config.MAX_FILE_SIZE
            async with splitter.big_file_guard(big):
                path = await download_file(info.get("final_url") or url, job_dir,
                                           progress_cb=progress,
                                           filename=info.get("filename"))
                prompted, paths = await _prompt_or_split(
                    client, message.chat.id, [path],
                    info.get("filename", ""), job_dir, job=job)
            if not prompted:
                await job.edit("Uploading…", force=True)
                await send_files_routed(client, message.chat.id, paths,
                                        "uploader",
                                        caption=info.get("filename", ""),
                                        job=job,
                                        user_id=message.from_user.id
                                        if message.from_user else None)
        finally:
            if not prompted:
                cleanup_dir(job_dir)
        return prompted


async def _scrape_flow(client, message, url, job,
                   force_browser=False):
    """Static scrape, data: playlist, then the browser — unless
    force_browser (/browser command) skips straight to the browser."""
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        started = time.time()

        async def progress(done, total):
            job.check_cancelled()
            await job.edit(progress_text("Downloading…", done, total, started))

        async def _download_media(media_url, headers=None, playlist=None):
            """A found media URL -> downloaded paths (a stream playlist
            goes to yt-dlp, not to a text file). `headers` carry the
            browser session (Referer, User-Agent, Cookie) when the
            URL came from the browser — many hosts lock the stream
            to that session."""
            if playlist is None:
                playlist = is_playlist_url(media_url)
            if playlist:
                # The page embeds a stream manifest, not a file — let
                # yt-dlp fetch the real segments instead of saving the
                # playlist text.
                await job.edit(
                    "Found a stream playlist — downloading the video…")
                return await download(media_url, job_dir,
                                      progress_cb=progress,
                                      headers=headers,
                                      cancel_check=lambda: job.cancelled)
            path = await download_file(media_url, job_dir,
                                       progress_cb=progress,
                                       headers=headers or {"Referer": url})
            return [path]

        async def _browser_media():
            """The page rendered in a real browser — some players only
            reveal the video URL once their JavaScript has run (or
            their play button is clicked). Returns the media dict
            (url + playlist flag + session headers)."""
            await job.edit(
                "Opening the page in a browser to find the video…")
            try:
                return await browser_scan.find_media(
                    url, cancel_check=lambda: job.cancelled)
            except browser_scan.BrowserError as exc:
                raise scrape.ScrapeError(
                    "no embedded media URL found in the page "
                    f"(browser: {exc})") from exc

        prompted = False
        try:
            if force_browser:
                found = await _browser_media()
                paths = await _download_media(
                    found["url"], headers=found.get("headers"),
                    playlist=found.get("playlist"))
            else:
                try:
                    media_url = await scrape.find_media_url(url)
                except scrape.ScrapeError:
                    # No link in the static HTML — but the playlist
                    # itself may hide in a data: URI, which ffmpeg
                    # can still play.
                    try:
                        path = await scrape.download_playlist(
                            url, job_dir, progress_cb=progress)
                        paths = [path]
                    except scrape.ScrapeError:
                        # Last resort: render the page in a real
                        # browser.
                        found = await _browser_media()
                        paths = await _download_media(
                            found["url"], headers=found.get("headers"),
                            playlist=found.get("playlist"))
                else:
                    found = None
                    if scrape.is_image_url(media_url):
                        # The static scrape found only a thumbnail
                        # (og:image). The page may still hide a video
                        # the browser can reach — click-to-play players
                        # load nothing until clicked — so the browser
                        # gets its turn, and the thumbnail is only the
                        # fallback.
                        await job.edit(
                            "That page only shows a thumbnail — opening "
                            "it in a browser to find the video…")
                        try:
                            found = await browser_scan.find_media(
                                url, cancel_check=lambda: job.cancelled)
                        except browser_scan.BrowserError:
                            found = None  # keep the thumbnail; it beats nothing
                    if found:
                        paths = await _download_media(
                            found["url"], headers=found.get("headers"),
                            playlist=found.get("playlist"))
                    else:
                        paths = await _download_media(media_url)
            prompted, paths = await _prompt_or_split(
                client, message.chat.id, paths, url, job_dir, job=job)
            if not prompted:
                await job.edit("Uploading…", force=True)
                await send_files_routed(client, message.chat.id, paths, "uploader",
                                 caption=url, job=job,
                                 user_id=message.from_user.id
                                 if message.from_user else None)
        finally:
            if not prompted:
                cleanup_dir(job_dir)
        return prompted


async def _instagram_carousel(client, message, url, job) -> bool:
    """Image posts: yt-dlp metadata carries every carousel image URL."""
    try:
        info = await extract_info(url, ignore_no_formats=True)
    except YtdlpError:
        return False
    urls = carousel_image_urls(info)
    if not urls:
        return False
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        try:
            await job.edit(f"Found {len(urls)} images — downloading…")
            paths = []
            for i, img_url in enumerate(urls):
                job.check_cancelled()
                paths.append(await download_file(
                    img_url, job_dir, filename=f"instagram_{i + 1}.jpg"))
            await send_files_routed(client, message.chat.id, paths, "uploader",
                             caption=url, job=job,
                             user_id=message.from_user.id
                             if message.from_user else None)
            return True
        finally:
            cleanup_dir(job_dir)


@Client.on_message(filters.command("browser") & filters.private
                   & admin_only)
async def browser_cmd(client: Client, message: Message):
    """Manual override: skip the whole chain, render the page in a
    real browser (with click-to-play) and download what it finds."""
    if not browser_scan.BROWSER_AVAILABLE:
        await message.reply_text(
            "The browser scraper isn't installed in this build, so "
            "`/browser` can't run. On a host with it installed, try "
            "again.")
        return
    urls = _extract_urls(message)
    if not urls:
        await message.reply_text(
            "Usage: `/browser <link>` — I'll open the page in a real "
            "browser and download the video it loads (or shows after "
            "its play button is clicked).")
        return
    urls = urls[:MAX_MULTI_LINKS]
    total = len(urls)
    for i, url in enumerate(urls, 1):
        job = await jobs.register(
            client, message.chat.id, _job_label(url), reply_to=message)
        try:
            await _scrape_flow(client, message, url, job,
                               force_browser=True)
        except JobCancelled:
            await job.fail("⏹ Cancelled.")
        except scrape.ScrapeError as exc:
            await job.fail(
                f"❌ The browser found no video: {exc}\n\n"
                "If that page needs a login or a captcha, open it in "
                "your own browser, download the file there, and send "
                "it to me instead.")
        except SizeBlocked as exc:
            await job.fail(f"❌ {exc}")
        except Exception as exc:
            await job.fail(f"❌ Failed: {exc}")
        finally:
            await job.finish()


@Client.on_message(filters.command("gallery") & filters.private
                   & admin_only)
async def gallery_cmd(client: Client, message: Message):
    """Manual override: run gallery-dl on the link, nothing else."""
    url = _extract_url(message)
    if not url:
        await message.reply_text(
            "Usage: `/gallery <link>` — I'll fetch it with gallery-dl "
            "only (galleries, image posts, multi-image pages).")
        return
    job = await jobs.register(client, message.chat.id,
                              _job_label(url), reply_to=message)
    try:
        await _gallery_flow(client, message, url, job)
    except JobCancelled:
        await job.fail("⏹ Cancelled.")
    except gallery_engine.GalleryError as exc:
        await job.fail(f"❌ gallery-dl found nothing: {exc}")
    except Exception as exc:
        await job.fail(f"❌ Failed: {exc}")
    finally:
        await job.finish()


# ---------------------------------------------------------------- entry point

@Client.on_message(filters.private & admin_only & filters.text
                   & ~filters.command(["start", "help", "batch", "cancel",
                                       "mediainfo", "live", "pipe",
                                       "vidsplit", "browser", "gallery",
                                       "joinzip"]))
def _job_label(url: str) -> str:
    """One line naming a link in the shared progress message:
    the site plus the file name when the URL carries one."""
    host = urlparse(url).netloc or url
    host = host[4:] if host.startswith("www.") else host
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    name = clean_display(name) or name
    label = f"{host} / {name}" if name else host
    return label[:40]


async def _run_links(client: Client, message: Message,
                     urls: list[str]) -> None:
    """Run links through _dispatch, one shared progress line each.

    Shared by url_handler (links in a chat message) and the .txt
    file handler (plugins/filetolink.py): one after another — each
    link is a full download + upload job, and running them in
    parallel would fight over the 2 CPUs and the job slots. A
    failed link never stops the rest; a "pending" link (a picker
    is on screen) resolves through its own button callback, so
    the loop moves on; a "cancelled" link (stopped by the owner)
    is neither a success nor a failure; a "blocked" link
    (bandwidth cap) stops the loop, since the rest would block
    too. The final reaction (👍 all done, 👎 all failed) goes on
    the source message — "pending" and "cancelled" get none.
    """
    outcomes = []
    for url in urls:
        job = await jobs.register(client, message.chat.id,
                                  _job_label(url), reply_to=message)
        try:
            outcome = await _dispatch(client, message, url, job)
        finally:
            await job.finish()
        outcomes.append(outcome)
        if outcome == "done":
            stats.record_job("uploader", ok=True)
        elif outcome == "failed":
            stats.record_job("uploader", ok=False)
        elif outcome == "blocked":
            break  # bandwidth cap: the rest would block too
    settled = [o for o in outcomes if o in ("done", "failed")]
    if settled and all(o == "done" for o in settled):
        await react(message, "👍")
    elif settled and all(o == "failed" for o in settled):
        await react(message, "👎")


async def url_handler(client: Client, message: Message):
    urls = _extract_urls(message)
    if not urls:
        return
    await react(message, "👀")
    if len(urls) > MAX_MULTI_LINKS:
        await message.reply_text(
            f"Taking the first {MAX_MULTI_LINKS} links — the rest were "
            "skipped. Send them in another message.")
        urls = urls[:MAX_MULTI_LINKS]
    await _run_links(client, message, urls)


async def _upage_local_flow(client: Client, message: Message, url: str,
                            job) -> str:
    """Upload a web-upload file to Telegram from the disk copy.

    The link is single-use by design: the file is deleted the moment
    this upload succeeds, so the same link can never upload twice.
    A failed Telegram upload leaves the file (and the link) intact
    for a retry.
    """
    token = urlparse(url).path[len("/u/"):].split("/")[0].split("?")[0]
    try:
        fid = upage.verify_download_token(token)
    except upage.TokenError:
        await job.fail(
            "❌ That upload link is invalid or expired. Make a fresh "
            "one with /fileuploadpage.")
        return "failed"
    rec = upage.lookup_file(fid)
    if rec is None:
        await job.fail(
            "❌ That upload link was already used or expired — the "
            "file is gone from the server. Upload it again with /fileuploadpage.")
        return "failed"
    path, name, size = rec["path"], rec["name"], rec["size"]
    if not os.path.exists(path):
        upage.consume(fid)
        await job.fail(
            "❌ The file is no longer on the server. Upload it again "
            "with /fileuploadpage.")
        return "failed"
    # Admin delivery (v1.24.0): no matter WHO uploads via /fileuploadpage,
    # the file lands in the OWNER's bot PM — not in the uploader's
    # chat, and the owner never has to touch the link himself.
    # The uploader still sends the link back (that is what triggers
    # the upload), but the destination is the owner, not the sender.
    # This bypasses /destination on purpose: the owner asked for
    # "his bot pm", not Saved Messages, for upage files.
    sender_is_owner = is_owner_id(
        message.from_user.id if message.from_user else None)
    if sender_is_owner:
        target_chat = message.chat.id
        note_to_sender = None
    else:
        target_chat = config.OWNER_ID
        sender_name = (
            (message.from_user.first_name or "")
            if message.from_user else "")
        note_to_sender = (
            f"✅ {name} ({human_bytes(size)}) is now on Telegram "
            f"in the admin's bot PM, and the server copy is deleted.")
    await job.edit(
        f"Uploading {name} ({human_bytes(size)}) from your web upload…")
    try:
        if sender_is_owner:
            await send_files_routed(client, target_chat, [path],
                                    "uploader", caption=name, job=job,
                                    user_id=message.from_user.id
                                    if message.from_user else None)
        else:
            # Tell the owner who sent it, before the file itself,
            # so the file does not arrive anonymously.
            try:
                sender_label = sender_name or "a sudo user"
                await client.send_message(
                    target_chat,
                    f"📤 Upage upload from {sender_label} — file below:")
            except Exception:
                pass
            await send_files(client, target_chat, [path], caption=name,
                             job=job)
    except JobCancelled:
        await job.fail("⏹ Cancelled.")
        return "cancelled"
    except Exception as exc:
        await job.fail(
            f"❌ Telegram upload failed: {exc}\nThe file is still on "
            "the server — you can retry this link.")
        return "failed"
    upage.consume(fid)
    await job.finish(
        note_to_sender or
        f"✅ {name} ({human_bytes(size)}) is now on Telegram, and the "
        f"server copy is deleted.")
    return "done"


async def _dispatch(client: Client, message: Message, url: str,
                    job) -> str:
    """Run the resolver chain.

    Returns "done" | "pending" | "failed" | "blocked" (bandwidth cap)
    | "cancelled" (stopped by the owner). `job` is the shared
    progress line for this link (see core/jobs).
    """
    if stats.bandwidth_blocked():
        await job.fail(stats.bandwidth_pause_text())
        return "blocked"
    errors: list[str] = []
    try:
        # 0) Own web-upload links: the file is ALREADY on this disk.
        # Send it straight to Telegram — downloading it again would
        # waste disk, bandwidth and time for zero benefit.
        if config.BASE_URL:
            parsed = urlparse(url)
            base = urlparse(config.BASE_URL)
            if parsed.netloc == base.netloc \
                    and parsed.path.startswith("/u/"):
                return await _upage_local_flow(client, message, url, job)

        # 1) Terabox gets its own extractor, always first for its domains.
        if terabox.is_terabox_url(url):
            try:
                picker = await _terabox_flow(client, message, url, job)
                return "pending" if picker else "done"
            except SizeBlocked as exc:
                await job.fail(f"❌ {exc}")
                return "failed"
            except Exception as exc:
                await job.fail(f"Terabox failed: {exc}")
                return "failed"

        # 1b) Custom-site extractors (luluvido etc.) — sites yt-dlp
        # does not understand, before yt-dlp wastes a turn on them.
        site = site_registry.match(url)
        if site:
            try:
                prompted = await _custom_flow(client, message, url,
                                              job, site)
                return "pending" if prompted else "done"
            except SizeBlocked as exc:
                await job.fail(f"❌ {exc}")
                return "failed"
            except FallThrough as exc:
                # Matched the URL but not the media (a link post, a
                # deleted post, an API hiccup) — the next engine gets
                # its turn; the reason joins the final error list.
                errors.append(
                    f"{site.__name__.rsplit('.', 1)[-1]}: {exc}")
            except Exception as exc:
                await job.fail(
                    f"{site.__name__.rsplit('.', 1)[-1]} failed: {exc}")
                return "failed"

        is_post = "instagram.com/p/" in url
        is_reel = "instagram.com/reel" in url

        # 2) Instagram photo posts are images, not videos.
        if is_post:
            if await _instagram_carousel(client, message, url, job):
                return "done"
            try:
                prompted = await _gallery_flow(client, message, url, job)
                return "pending" if prompted else "done"
            except Exception as exc:
                errors.append(f"gallery-dl: {exc}")

        # 3) A raw file link needs no extractor at all.
        direct_info = await check_direct(url)
        if direct_info:
            try:
                check_size(direct_info.get("size") or 0,
                           direct_info.get("filename") or "file",
                           allow_split=True)
            except SizeBlocked as exc:
                await job.fail(f"❌ {exc}")
                return "failed"
            try:
                prompted = await _direct_flow(client, message, url, direct_info,
                                              job)
                return "pending" if prompted else "done"
            except Exception as exc:
                errors.append(f"direct: {exc}")

        # 4) yt-dlp — reels and every other video/audio site.
        try:
            picker = await _ytdlp_flow(client, message, url, job,
                                        ask_format=not is_reel)
            return "pending" if picker else "done"
        except SizeBlocked as exc:
            await job.fail(f"❌ {exc}")
            return "failed"
        except Exception as exc:
            errors.append(f"yt-dlp: {exc}")

        # 5) gallery-dl also covers many video-page galleries as backup.
        if not is_post:
            try:
                prompted = await _gallery_flow(client, message, url, job)
                return "pending" if prompted else "done"
            except SizeBlocked as exc:
                # A file gallery-dl downloaded turned out too big to
                # split — retrying it through the scrape would just
                # download the same giant file a second time.
                await job.fail(f"❌ {exc}")
                return "failed"
            except Exception as exc:
                errors.append(f"gallery-dl: {exc}")

        # 6) Last resort: scrape the page's meta tags — and, if the
        # static HTML holds nothing, a real browser.
        try:
            prompted = await _scrape_flow(client, message, url, job)
            return "pending" if prompted else "done"
        except SizeBlocked as exc:
            await job.fail(f"❌ {exc}")
            return "failed"
        except Exception as exc:
            errors.append(f"scrape: {exc}")

        hint = ("\n\nIf it's Instagram/a login-walled site, it may need a "
                "cookies.txt — see the README.")
        if "reddit.com" in url and any(
                "blocked by network security" in e for e in errors):
            hint = ("\n\n⚠️ Reddit is blocking this server (\"blocked by "
                    "network security\"). Fix: log into reddit.com in your "
                    "browser, export a cookies.txt, and send it to me with "
                    "/setcookies as the caption — I'll browse as you from "
                    "then on.")
        if ("youtube.com" in url or "youtu.be" in url) and any(
                "not a bot" in e for e in errors):
            hint = ("\n\n⚠️ YouTube is challenging this server (\"not a "
                    "bot\"), cookies or not. Try in order: 1) /updateytdlp — "
                    "YouTube breaks old yt-dlp constantly; 2) re-export "
                    "fresh YouTube cookies while logged in — /setcookies → "
                    "YouTube warns if the file isn't a logged-in session; "
                    "3) cookies from a different Google account — YouTube "
                    "sometimes flags the account+IP combo.")
        await job.fail(
            "I couldn't download that with any engine:\n"
            + "\n".join(f"• {e}" for e in errors)
            + hint)
        return "failed"
    except JobCancelled:
        # Stopped by the owner mid-chain: the shared line clears and
        # a short note says so. Not a failure — no 👎, no failed stat.
        await job.fail("⏹ Cancelled.")
        return "cancelled"
    except Exception as exc:  # never leave a stuck progress line
        try:
            await job.fail(f"Something went wrong: {exc}")
        except Exception:
            pass
        return "failed"


def _check_picked_format(job: dict, choice: str) -> dict | None:
    """Refuse a quality-button tap when that format is too big.

    Raises SizeBlocked; the callback's except turns it into a
    "Download failed: …" note. Returns the picked format dict
    (None for audio-only / no formats) so the caller can tell a
    big-file download from a normal one.
    """
    formats = job.get("formats") or []
    if not formats or choice == "audio":
        return None  # audio-only size is unknown until the mp3 exists
    if choice == "best":
        picked = formats[0]
    else:
        picked = next((f for f in formats if f["format_id"] == choice), None)
    if picked:
        check_size(picked.get("filesize") or 0,
                   job.get("title") or "file", allow_split=True)
    return picked


# ---------------------------------------------------------------- callbacks

@Client.on_callback_query(filters.regex(r"^unzip:"))
async def unzip_callback(client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer("Stale button.")
        return
    job_id, choice = parts[1], parts[2]
    _prune_unzip_pending()
    # Picker taps (v1.45.0) don't consume the job — they just flip
    # a tick and re-render the prompt in place.
    if choice in ("t", "all", "none"):
        job = PENDING_UNZIP.get(job_id)
        if not job:
            await query.answer("This unzip job expired — send the link "
                               "again.", show_alert=True)
            return
        listed = min(len(job["entries"]), _MAX_LISTED_FILES)
        if choice == "t" and len(parts) >= 4 and parts[3].isdigit():
            idx = int(parts[3])
            if 0 <= idx < listed:
                if idx in job["selected"]:
                    job["selected"].discard(idx)
                else:
                    job["selected"].add(idx)
                job["touched"] = True
        elif choice == "all":
            job["selected"] = set(range(listed))
            job["touched"] = True
        elif choice == "none":
            job["selected"] = set()
            job["touched"] = True
        try:
            await query.message.edit_text(
                _unzip_prompt_text(job),
                reply_markup=_unzip_prompt_markup(job))
        except Exception:
            pass
        await query.answer()
        return
    if choice == "go":
        # Nothing ticked: refuse with a hint instead of unpacking
        # an empty set (and keep the prompt alive).
        job = PENDING_UNZIP.get(job_id)
        if job and job["touched"] and not job["selected"]:
            await query.answer("Tick at least one file first.",
                               show_alert=True)
            return
    job = PENDING_UNZIP.pop(job_id, None)
    if not job:
        await query.answer("This unzip job expired — send the link again.",
                           show_alert=True)
        return
    status = query.message
    user_id = query.from_user.id if query.from_user else None
    job_dir, zip_path = job["job_dir"], job["path"]
    try:
        if choice == "asis":
            await query.answer("Sending the archive…")
            await status.edit_text("Uploading the archive…")
            await send_files_split(client, job["chat_id"], [zip_path],
                                   "uploader", caption=job["caption"],
                                   status_msg=status, user_id=user_id)
        else:
            await query.answer("Unpacking…")
            await status.edit_text("Unpacking…")
            extract_dir = os.path.join(job_dir, "extracted")
            try:
                info = unzip.archive_info(zip_path)
            except Exception:
                info = {"count": 0}
            # Tick-selected files only (v1.45.0): when the owner
            # touched the picker, only the ticked entries come
            # out — unlisted files beyond the first 10 are skipped
            # too. Untouched, everything comes out as before.
            only_names = None
            total = info["count"]
            if job.get("touched"):
                only_names = {job["entries"][i][0]
                              for i in job["selected"]}
                total = len(only_names)
            # One by one: extract one file, upload it, delete
            # it, then extract the next — disk never holds
            # more than the archive plus one file.
            skipped: list[str] = []
            failed: list[str] = []
            sent_count = 0
            bandwidth_stopped = False
            gen = unzip.iter_extracted(zip_path, extract_dir, skipped,
                                       only_names)
            try:
                for path in gen:
                    if stats.bandwidth_blocked():
                        bandwidth_stopped = True
                        break
                    try:
                        await status.edit_text(
                            f"Uploading {sent_count + 1} of "
                            f"{total}: "
                            f"{os.path.basename(path)}…")
                    except Exception:
                        pass
                    # Oversize files are split into .001/.002
                    # parts before sending (v1.40.0) — a 10 GB
                    # video inside the zip arrives as mkv.001,
                    # mkv.002, … instead of failing the cap.
                    sent_paths = [path]
                    try:
                        # Each file goes out under its OWN clean
                        # title ("Show S01E01"), not the archive's
                        # name (v1.44.0).
                        file_caption = (
                            clean_display(os.path.basename(path))
                            or job["caption"])
                        sent_paths = await send_files_split(
                            client, job["chat_id"], [path],
                            "uploader", caption=file_caption,
                            status_msg=status, user_id=user_id)
                        sent_count += 1
                    except Exception as exc:
                        failed.append(f"{os.path.basename(path)} ({exc})")
                    for sent in sent_paths:
                        try:
                            os.unlink(sent)
                        except OSError:
                            pass
                    # Pace one-by-one sends (see plugins/unzip.py).
                    await asyncio.sleep(1)
            finally:
                gen.close()
            if sent_count == 0 and not bandwidth_stopped:
                await status.edit_text(
                    "❌ Nothing could be unzipped from this file"
                    + (f" (skipped: {', '.join(skipped[:5])})"
                       if skipped else "")
                    + ".\nSending the zip itself as-is instead.")
                await send_files_split(client, job["chat_id"], [zip_path],
                                       "uploader", caption=job["caption"],
                                       status_msg=status, user_id=user_id)
                return
            if bandwidth_stopped:
                try:
                    await client.send_message(
                        job["chat_id"],
                        "⚠️ Unzip stopped early: monthly bandwidth "
                        "cap reached — remaining files were not sent.")
                except Exception:
                    pass
            if failed:
                try:
                    await client.send_message(
                        job["chat_id"],
                        "⚠️ Failed to send while unzipping: "
                        + ", ".join(failed[:5]))
                except Exception:
                    pass
            if skipped:
                try:
                    await client.send_message(
                        job["chat_id"],
                        "⚠️ Skipped while unzipping: "
                        + ", ".join(skipped[:10]))
                except Exception:
                    pass
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as exc:
        try:
            await status.edit_text(f"Unzip failed: {exc}")
        except Exception:
            pass
    finally:
        cleanup_dir(job_dir)


@Client.on_callback_query(filters.regex(r"^(fmt|tera):"))
async def format_callback(client: Client, query: CallbackQuery):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer("Stale button.")
        return
    kind, job_id = parts[0], parts[1]
    job = PENDING.pop(job_id, None)
    if not job:
        await query.answer("This job expired — send the link again.",
                           show_alert=True)
        return
    if stats.bandwidth_blocked():
        await query.answer("⛔ Bandwidth paused", show_alert=True)
        try:
            await query.message.edit_text(stats.bandwidth_pause_text())
        except Exception:
            pass
        return
    await query.answer("Starting…")
    # The pick is made — the picker message goes away and the
    # download registers a fresh line in the shared progress
    # message (v1.45.0).
    try:
        await query.message.delete()
    except Exception:
        pass
    user_id = query.from_user.id if query.from_user else None
    live = await jobs.register(client, job["chat_id"],
                               _job_label(job["url"]))
    fake_message = query.message  # carries chat.id for the send helpers

    try:
        if kind == "tera":
            index = int(parts[2])
            files = await terabox.list_files(job["url"])
            info = files[index] if index < len(files) else None
            if info:
                check_size(info.get("size") or 0, info.get("name") or "file",
                           allow_split=True)
            prompted = await _terabox_download(
                client, fake_message, job["url"],
                index, info, live,
                big_file=bool(info)
                and (info.get("size") or 0) > config.MAX_FILE_SIZE,
                user_id=user_id)
        else:
            choice = parts[2]
            picked = _check_picked_format(job, choice)
            prompted = await _ytdlp_download(
                client, fake_message, job["url"], job.get("title", "Download"),
                live,
                format_id=None if choice in ("best", "audio") else choice,
                audio_only=choice == "audio",
                big_file=bool(picked)
                and (picked.get("filesize") or 0) > config.MAX_FILE_SIZE,
                user_id=user_id)
        if prompted:
            # Unzip prompt is on screen; the unzip callback will
            # finish (and react to) the job. Do not mark it done yet.
            return
        if job.get("origin_chat") and job.get("origin_msg"):
            await react_to(client, job["origin_chat"], job["origin_msg"], "👍")
        stats.record_job("uploader", ok=True)
    except JobCancelled:
        await live.fail("⏹ Cancelled.")
    except Exception as exc:
        if job.get("origin_chat") and job.get("origin_msg"):
            await react_to(client, job["origin_chat"], job["origin_msg"], "👎")
        stats.record_job("uploader", ok=False)
        await live.fail(f"Download failed: {exc}")
    finally:
        await live.finish()


@Client.on_callback_query(filters.regex(r"^jb:stop:"))
async def stop_job_callback(client: Client, query: CallbackQuery):
    """⏹ Stop button on the shared progress message (v1.45.0).

    Flags the job cancelled — the download/upload loops notice
    within a second or two (progress callbacks raise JobCancelled,
    yt-dlp's hook aborts the transfer, gallery-dl's process is
    killed) and the job's line clears with a "Cancelled." note.
    Anyone admin may stop any job in the chat: the shared message
    is one per chat, and a stuck download blocks the job slots
    for everyone in it.
    """
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer("Stale button.")
        return
    job = jobs.get_job(parts[2])
    if job is None:
        await query.answer("That job already finished.")
        return
    job.cancel()
    await query.answer("Stopping…")
    await job.edit("⏹ Stopping…", force=True)
