"""One unpack run: archive -> Telegram, shared by the chat picker
and the zip file-manager page (v1.51.14).

The chat picker (plugins/uploader.py unzip_callback) used to own
this whole body. The web page needs the exact same run — same
shared progress message, same Stop button, same per-file sends,
same summary — so it lives here now and both callers are thin
wrappers around it. Behaviour is unchanged for the picker: the
tests in test_v1480.py drive the callback and assert the same
outcomes as before the move.

Modes:
  "send"  — extract the chosen files one by one, sending each as
            it comes out (disk never holds more than the archive
            plus one file), like the picker always did.
  "asis"  — send the archive itself, no unpacking.
  "rezip" — extract the chosen files, pack them into one new zip
            (stored, no compression — the files are media, deflate
            would only burn CPU) and send that instead.
"""
import asyncio
import logging
import os
import shutil
import zipfile

from core import jobs, stats, unzip
from core.filename_parser import clean_display
from core.jobs import JobCancelled
from core.sender import send_files_split
from core.utils import cleanup_dir, human_bytes

log = logging.getLogger(__name__)


def _summary(name: str, sent: int, total: int,
             skipped: list, failed: list, stopped: bool) -> str:
    """Final notice for an unpack run, sent as its own message
    (v1.48.0 — the run's status message used to just vanish)."""
    count = f"{sent} of {total}" if total else str(sent)
    text = f"✅ Unpacked {name}: sent {count} file(s)"
    if stopped:
        text += " — stopped early (bandwidth cap)"
    if skipped:
        text += f" · {len(skipped)} skipped"
    if failed:
        text += f" · {len(failed)} failed"
    return text + "."


async def _send_asis(client, chat_id, zip_path, caption, ujob, user_id):
    """The fallback when nothing could be unzipped: the archive
    itself goes out as-is."""
    await send_files_split(client, chat_id, [zip_path], "uploader",
                           caption=caption, job=ujob, user_id=user_id)


async def run_unpack(client, *, chat_id: int, zip_path: str,
                     job_dir: str, caption: str,
                     names, user_id, mode: str = "send",
                     label: str | None = None, reply_to=None,
                     prompt_message=None) -> None:
    """Run one unpack job to completion (or failure/cancel).

    `names` is the set of archive entry names to extract, or None
    for everything. `mode` is "send", "asis" or "rezip".
    The job dir is deleted when the run ends, whatever happened.
    """
    name = os.path.basename(zip_path)
    if label is None:
        label = "📦 " + (clean_display(name) or name)[:37]
    ujob = None
    sent_count = 0
    total = 0
    try:
        # The run joins the shared progress message (v1.48.0): one
        # line with a Stop button, and the final summary as its own
        # message. The caller answers its own tap first (Telegram
        # only waits so long); the picker message goes once the
        # job is registered.
        ujob = await jobs.register(client, chat_id, label,
                                   reply_to=reply_to)
        if prompt_message is not None:
            try:
                await prompt_message.delete()
            except Exception:
                pass
        if mode == "asis":
            await ujob.edit("Uploading the archive…")
            await _send_asis(client, chat_id, zip_path, caption,
                             ujob, user_id)
            await ujob.finish()
            return
        await ujob.edit("Unpacking…")
        try:
            info = unzip.archive_info(zip_path)
        except Exception:
            info = {"count": 0}
        total = info["count"]
        if names is not None:
            total = len(names)
        if mode == "rezip":
            await _run_rezip(client, chat_id, zip_path, job_dir,
                             caption, names, info, total,
                             user_id, ujob)
            return
        extract_dir = os.path.join(job_dir, "extracted")
        # Tick-selected files only (v1.45.0): when the owner
        # touched the picker, only the ticked entries come out —
        # unlisted files beyond the first 10 are skipped too.
        # Untouched, everything comes out as before.
        skipped: list[str] = []
        failed: list[str] = []
        bandwidth_stopped = False
        gen = unzip.iter_extracted(zip_path, extract_dir, skipped,
                                   names)
        try:
            # A manual next() loop (not `for path in gen`) so the
            # line can say which file is being unpacked BEFORE
            # the extraction starts — the for loop only learns
            # the name after the file is already out.
            while True:
                ujob.check_cancelled()
                if stats.bandwidth_blocked():
                    bandwidth_stopped = True
                    break
                await ujob.edit(
                    f"Unpacking file {sent_count + 1} of {total}…"
                    if total else "Unpacking…")
                try:
                    path = next(gen)
                except StopIteration:
                    break
                fname = os.path.basename(path)
                if total:
                    await ujob.edit(
                        f"Uploading {sent_count + 1} of {total}: "
                        f"{fname}…")
                    bar_label = (f"Uploading {sent_count + 1} of "
                                 f"{total}: {fname[:40]}")
                else:
                    await ujob.edit(f"Uploading {fname}…")
                    bar_label = f"Uploading {fname[:40]}"
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
                        clean_display(fname) or caption)
                    sent_paths = await send_files_split(
                        client, chat_id, [path],
                        "uploader", caption=file_caption,
                        job=ujob, user_id=user_id,
                        progress_label=bar_label)
                    sent_count += 1
                except Exception as exc:
                    failed.append(f"{fname} ({exc})")
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
            await ujob.edit(
                "❌ Nothing could be unzipped from this file"
                + (f" (skipped: {', '.join(skipped[:5])})"
                   if skipped else "")
                + ".\nSending the zip itself as-is instead.")
            await _send_asis(client, chat_id, zip_path, caption,
                             ujob, user_id)
            await ujob.finish(
                f"📦 Nothing unzipped from "
                f"{name} — sent the archive "
                f"as-is.")
            return
        if bandwidth_stopped:
            try:
                await client.send_message(
                    chat_id,
                    "⚠️ Unzip stopped early: monthly bandwidth "
                    "cap reached — remaining files were not sent.")
            except Exception:
                pass
        if failed:
            try:
                await client.send_message(
                    chat_id,
                    "⚠️ Failed to send while unzipping: "
                    + ", ".join(failed[:5]))
            except Exception:
                pass
        if skipped:
            try:
                await client.send_message(
                    chat_id,
                    "⚠️ Skipped while unzipping: "
                    + ", ".join(skipped[:10]))
            except Exception:
                pass
        await ujob.finish(_summary(name, sent_count, total,
                                   skipped, failed,
                                   bandwidth_stopped))
    except JobCancelled:
        # Stopped by the owner (Stop button or the run's own check):
        # the shared line clears and a short note says how far it
        # got. fail() sends the note as its own message.
        if ujob is not None:
            count = (f" — sent {sent_count} of {total} files."
                     if total else f" — sent {sent_count} file(s).")
            await ujob.fail(f"⏹ Unzip cancelled{count}")
    except Exception as exc:
        if ujob is not None:
            await ujob.fail(f"Unzip failed: {exc}")
        else:
            # The job never registered (answer/registration failed):
            # the picker message is still there — say so on it.
            if prompt_message is not None:
                try:
                    await prompt_message.edit_text(f"Unzip failed: {exc}")
                except Exception:
                    pass
    finally:
        cleanup_dir(job_dir)


async def _run_rezip(client, chat_id, zip_path, job_dir, caption,
                     names, info, total, user_id, ujob) -> None:
    """Extract the chosen files, pack them into one new zip and
    send that. Called with the job already registered."""
    name = os.path.basename(zip_path)
    entries = info.get("entries") or []
    chosen = [(n, s) for n, s in entries
              if names is None or n in names]
    selected_total = sum(s for _n, s in chosen)
    # The rezip holds the extracted files AND the new zip at once,
    # so it needs roughly twice the selection on disk. The
    # one-by-one send never holds the total, which is why the
    # prompt only checks the largest file — here the total is the
    # check.
    free = shutil.disk_usage(job_dir).free
    if free < selected_total * 2 + 64 * 1024 * 1024:
        await ujob.fail(
            "❌ Not enough free space to re-zip "
            f"{human_bytes(selected_total)} — send the files "
            "unpacked instead.")
        return
    if stats.bandwidth_blocked():
        try:
            await client.send_message(
                chat_id,
                "⚠️ Unzip stopped early: monthly bandwidth "
                "cap reached — remaining files were not sent.")
        except Exception:
            pass
        await ujob.finish(_summary(name, 0, total, [], [], True))
        return
    extract_dir = os.path.join(job_dir, "extracted")
    stage_dir = os.path.join(job_dir, "staged")
    os.makedirs(stage_dir, exist_ok=True)
    skipped: list[str] = []
    # One generator per entry, staged at once: iter_extracted
    # deletes the file it yielded on the next extraction (and on
    # close), so a collected list of yielded paths would point at
    # deleted files. Each file is moved to the stage dir before
    # its generator is closed; the arcname keeps the archive's
    # own folder path.
    staged: list[tuple[str, str]] = []
    for entry_name, _size in chosen:
        ujob.check_cancelled()
        await ujob.edit(
            f"Unpacking file {len(staged) + 1} of {total}…"
            if total else "Unpacking…")
        gen = unzip.iter_extracted(zip_path, extract_dir, skipped,
                                   {entry_name})
        try:
            path = next(gen)
        except StopIteration:
            gen.close()
            continue
        arc = entry_name.replace("\\", "/")
        base = arc.rsplit("/", 1)[-1]
        stage_path = os.path.join(stage_dir,
                                  f"{len(staged)}-{base}")
        try:
            os.rename(path, stage_path)
        except OSError:
            shutil.copyfile(path, stage_path)
            try:
                os.unlink(path)
            except OSError:
                pass
        gen.close()
        staged.append((stage_path, arc))
    if not staged:
        await ujob.edit(
            "❌ Nothing could be unzipped from this file"
            + (f" (skipped: {', '.join(skipped[:5])})"
               if skipped else "")
            + ".\nSending the zip itself as-is instead.")
        await _send_asis(client, chat_id, zip_path, caption,
                         ujob, user_id)
        await ujob.finish(
            f"📦 Nothing unzipped from {name} — sent the archive "
            f"as-is.")
        return
    stem = os.path.splitext(name)[0]
    new_zip = os.path.join(job_dir, f"{stem}-selection.zip")
    await ujob.edit(f"Zipping {len(staged)} file(s)…")
    used: set[str] = set()
    with zipfile.ZipFile(new_zip, "w", zipfile.ZIP_STORED) as zf:
        for path, arc in staged:
            # A zip can hold two entries under one name; the
            # second would overwrite the first on extract.
            base, ext = os.path.splitext(arc)
            n = 2
            while arc in used:
                arc = f"{base} ({n}){ext}"
                n += 1
            used.add(arc)
            zf.write(path, arc)
    for path, _arc in staged:
        try:
            os.unlink(path)
        except OSError:
            pass
    zip_size = os.path.getsize(new_zip)
    title = clean_display(name) or caption
    sent_paths = await send_files_split(
        client, chat_id, [new_zip], "uploader",
        caption=f"{title} (selection)", job=ujob, user_id=user_id)
    for sent in sent_paths:
        try:
            os.unlink(sent)
        except OSError:
            pass
    text = (f"✅ Unpacked {name}: sent {len(staged)} file(s) "
            f"as one zip ({human_bytes(zip_size)}).")
    if skipped:
        text += f" · {len(skipped)} skipped"
    await ujob.finish(text)
