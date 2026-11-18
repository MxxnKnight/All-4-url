""" /metadata — write tags (title, artist, …) into a file.

Reply to a video or audio file with e.g.
    /metadata title=My Movie | artist=Moon
and the bot downloads the file, writes the tags into a copy with
ffmpeg (stream copy — the video/audio itself is NOT re-encoded,
so it is fast and lossless), and sends the tagged file back.

This is the WZML-X /metadata idea, reimplemented: WZML-X's
command also *writes* tags rather than showing them — probing is
still /mediainfo's job.

Format: key=value pairs separated by `|`; a literal pipe inside a
value is written `\\|`. Keys are ffmpeg metadata keys (title,
artist, album, genre, comment, date, composer, …). Empty values
are skipped. No pairs → usage text.

Limits, honestly:
  * Video/audio files only (a document that IS a video/audio file
    works — the content is probed after download, not the name).
  * The file is fully rewritten, so the server briefly holds two
    copies — a file that doesn't fit twice on disk is refused.
  * Tags the container can't hold are dropped by ffmpeg silently
    (e.g. some keys on some containers); the file still sends.
"""

import os
import shutil
import subprocess
import time

from pyrogram import Client, filters

from core import sender, stats
from core.utils import (JOB_SEMAPHORE, ThrottledEditor, admin_only,
                        cleanup_dir, guess_kind, human_bytes, make_job_dir,
                        progress_text, run_blocking)

#: Disk headroom kept free after the input + output copies (256 MB,
#: same as the unzip flow).
_HEADROOM_BYTES = 256 * 1024 * 1024


def parse_metadata_spec(text: str) -> dict:
    """'title=Foo | artist=Bar' -> {'title': 'Foo', 'artist': 'Bar'}.

    A literal pipe inside a value is written `\\|`. Parts without
    '=' and empty keys/values are skipped.
    """
    if not text or not isinstance(text, str):
        return {}
    parts, current, i = [], "", 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            current += "|"
            i += 2
        elif text[i] == "|":
            parts.append(current)
            current = ""
            i += 1
        else:
            current += text[i]
            i += 1
    if current:
        parts.append(current)
    out = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def _remux_with_metadata(src: str, dst: str, tags: dict) -> None:
    """ffmpeg stream-copy remux writing `tags` (raises on failure)."""
    cmd = ["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy"]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(dst)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not os.path.isfile(dst):
        tail = (proc.stderr or "").strip().splitlines()[-1:]
        raise RuntimeError(tail[0] if tail else "ffmpeg remux failed")


_USAGE = ("Reply to a video or audio file with e.g.\n"
          "`/metadata title=My Movie | artist=Moon`\n\n"
          "I write those tags into a copy and send it back — the "
          "video/audio itself is not re-encoded. Keys are separated "
          "by `|`; write a literal pipe as `\\|`.")


@Client.on_message(filters.private & admin_only
                   & filters.command("metadata"))
async def metadata_cmd(client: Client, message):
    replied = message.reply_to_message
    if replied is None:
        await message.reply_text(_USAGE)
        return
    spec = (message.text or "").split(None, 1)
    tags = parse_metadata_spec(spec[1] if len(spec) > 1 else "")
    if not tags:
        await message.reply_text(_USAGE)
        return
    media = None
    for kind in ("video", "audio", "document"):
        media = getattr(replied, kind, None)
        if media is not None:
            break
    if media is None:
        await message.reply_text(
            "Metadata only works for video and audio files. Reply to "
            "one with /metadata.")
        return
    name = os.path.basename(
        getattr(media, "file_name", None) or "file") or "file"
    size = int(getattr(media, "file_size", 0) or 0)
    if stats.bandwidth_blocked():
        await message.reply_text(stats.bandwidth_pause_text())
        return
    status = await message.reply_text(
        f"Writing tags into **{name}**…")
    editor = ThrottledEditor(status)
    async with JOB_SEMAPHORE:
        job_dir = make_job_dir()
        try:
            started = time.time()

            async def progress(done, total):
                await editor.edit(progress_text("Downloading…",
                                                done, total, started))

            src = await client.download_media(
                replied, file_name=os.path.join(job_dir, name),
                progress=progress)
            if not src or not os.path.isfile(src):
                await status.edit_text("❌ Download failed — nothing arrived.")
                return
            # The file must BE media, whatever its name says.
            if guess_kind(src) == "photo":
                await status.edit_text(
                    "❌ That's a photo — metadata only works for video "
                    "and audio files.")
                return
            meta = await run_blocking(sender._ffprobe, src)
            if meta.get("probed") and not (
                    meta.get("has_video") or meta.get("has_audio")):
                await status.edit_text(
                    "❌ That file has no video or audio streams in it — "
                    "nothing to tag.")
                return
            # Input + output copies must fit with headroom.
            free = shutil.disk_usage(job_dir).free
            need = 2 * os.path.getsize(src) + _HEADROOM_BYTES
            if free < need:
                await status.edit_text(
                    f"❌ Not enough free disk to tag this file "
                    f"({human_bytes(need)} needed).")
                return
            dst = os.path.join(job_dir, "tagged_" + os.path.basename(src))
            await editor.edit("Writing tags…", force=True)
            try:
                await run_blocking(_remux_with_metadata, src, dst, tags)
            except Exception as exc:
                await status.edit_text(f"❌ ffmpeg could not tag it: {exc}")
                return
            await editor.edit("Uploading…", force=True)
            # The tagged file keeps the original name on Telegram.
            final = os.path.join(job_dir, os.path.basename(src))
            if dst != final:
                os.replace(dst, final)
            await sender.send_files(
                client, message.chat.id, [final],
                caption=f"{name}", status_msg=status)
            await status.delete()
        finally:
            cleanup_dir(job_dir)
