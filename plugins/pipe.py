""" /pipe <link> — stream a file straight to Telegram, no disk.

Two routes share this one command, so the owner does not
have to learn which kind of link they hold:

  * Direct file links: the source is read over HTTP in
    chunks as Telegram asks for it (see core/pipe.py for the
    file-like object and the WzGram save_file contract it
    satisfies). One download, one upload.
  * HLS (.m3u8) / DASH (.mpd) playlists: ffmpeg reads the
    playlist, fetches the segments, and remuxes them
    (stream-copy, no re-encode) into one fragmented mp4 on
    stdout. Because Telegram needs the total size upfront
    and ffmpeg does not know it until it finishes, /pipe
    runs ffmpeg twice: a measurement pass that counts
    stdout bytes (and discards them), then an upload pass
    that streams the same command with the measured size.
    That is two downloads and one upload — inbound is free
    on Koyeb, so the cost is time, not bandwidth cap — and
    it only works for VOD playlists (a live playlist has no
    end, no size, and changes between passes, so it is
    refused upfront).

Both routes land in the chat (or Saved Messages, per
/destination) as a document — no thumbnail, because a
thumbnail needs a file to probe, which a pipe deliberately
does not have. Neither route sends video pages or yt-dlp
links: those need an extractor first, which is the normal
uploader's job, not a chunk pipe.

If the source dies mid-upload, there is no partial message:
the upload fails, the status message says so, and the owner
retries /pipe (if the link is still alive) or sends the link
normally (disk path).

Bandwidth: a pipe still counts its Telegram upload once
against the monthly outbound guard (download from the
source is inbound and does not count). It is checked before
starting, like every other job.
"""

import asyncio
import time

from pyrogram import Client, filters

import config
from core import settings, stats
from core.pipe import (FfmpegFile, PipeError, RemoteFile,
                       inspect_playlist, is_playlist_url,
                       measure_ffmpeg_output, probe)
from core.utils import (JOB_SEMAPHORE, ThrottledEditor, admin_only,
                        human_bytes, human_time, progress_text,
                        react, run_blocking)

USAGE = (
    "Usage: `/pipe <link>`\n\n"
    "Streams a file straight to Telegram — nothing is saved "
    "on the server, so files that would fill the 2 GB disk "
    "can still land.\n\n"
    "• Direct file links, or HLS (.m3u8) / DASH (.mpd) "
    "playlist links — one file, ≤ 2 GB\n"
    "• Sent as a document (no video thumbnail — a thumbnail "
    "needs a file to probe)\n"
    "• HLS/DASH pipes download twice (once to measure the "
    "size, once to upload) and refuse live streams\n"
    "• NOT video pages or yt-dlp links — those need the "
    "normal uploader"
)


def _progress_callback(editor: ThrottledEditor):
    started = time.time()

    async def callback(current: int, total: int):
        await editor.edit(progress_text("Piping to Telegram…",
                                        current, total, started))
    return callback


async def _send_piped(client, message, file_obj, filename, status,
                      editor) -> object | None:
    """Send one pipe file object, honouring /destination.

    Returns the sent Message, or None if the upload was
    stopped. The caller closes file_obj and records stats.
    """
    send_client, chat_id = client, message.chat.id
    if settings.get_dest("uploader") == "saved":
        try:
            from core.userclient import SessionNotConfigured, get_user_client
            user = await get_user_client()
            send_client, chat_id = user, "me"
        except SessionNotConfigured:
            pass
    return await send_client.send_document(
        chat_id, file_obj, file_name=filename,
        caption=f"📦 {filename}",
        progress=_progress_callback(editor))


@Client.on_message(filters.command("pipe") & filters.private & admin_only)
async def pipe_command(client, message):
    if stats.bandwidth_blocked():
        await message.reply_text(stats.bandwidth_pause_text())
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text(USAGE)
        return
    url = parts[1].strip()
    await react(message, "👀")
    status = await message.reply_text("Probing the link…")
    try:
        async with JOB_SEMAPHORE:
            if stats.bandwidth_blocked():
                await status.edit_text(stats.bandwidth_pause_text())
                return
            editor = ThrottledEditor(status)
            if is_playlist_url(url):
                await _pipe_playlist(client, message, url, status, editor)
                return
            try:
                info = await run_blocking(probe, url)
            except PipeError as exc:
                await status.edit_text(f"❌ /pipe can't use that link: {exc}")
                return
            if info.get("is_playlist"):
                # A playlist hiding behind a non-.m3u8/.mpd URL
                # (revealed by its Content-Type): route it to
                # ffmpeg instead of sending the playlist file.
                await _pipe_playlist(client, message, info["url"],
                                     status, editor)
                return
            size, filename = info["size"], info["filename"]
            await editor.edit(
                f"Piping {filename}\n{human_bytes(size)} — "
                f"no disk, straight to Telegram…", force=True)
            remote = RemoteFile(info["url"], size, filename)
            try:
                sent = await _send_piped(client, message, remote,
                                         filename, status, editor)
            finally:
                remote.close()
            if sent is None:
                await status.edit_text(
                    "Upload was stopped before it finished.")
                stats.record_job("pipe", ok=False)
                return
            stats.record("uploaded", size)
            stats.record("downloaded", size)
            stats.record_job("pipe", ok=True)
            await status.edit_text(
                f"✅ Piped {filename} ({human_bytes(size)}) — "
                f"nothing touched the server disk.")
            await react(message, "👍")
    except Exception as exc:
        # A mid-upload source death lands here (RemoteFile /
        # FfmpegFile raise OSError; WzGram re-raises it after
        # its part retries). No partial message exists in the
        # chat, so the honest recovery is a retry, not a cleanup.
        try:
            await status.edit_text(
                f"❌ Pipe failed: {exc}\n\n"
                f"If the link is still alive, try /pipe again — "
                f"or send the link normally for the disk path.")
        except Exception:
            pass
        stats.record_job("pipe", ok=False)


async def _pipe_playlist(client, message, url, status, editor):
    """The ffmpeg route for one HLS/DASH playlist link."""
    try:
        info = await run_blocking(inspect_playlist, url)
    except PipeError as exc:
        await status.edit_text(f"❌ /pipe can't use that link: {exc}")
        return
    # Measurement pass, with a watcher editing the status
    # message from the shared counter (the measurement itself
    # runs in a blocking thread and cannot edit Telegram).
    counter = {"bytes": 0}
    done = {"flag": False}

    async def _watch():
        started = time.time()
        while not done["flag"]:
            await asyncio.sleep(3.5)
            if done["flag"]:
                break
            await editor.edit(
                f"Measuring {info['filename']}…\n"
                f"Pass 1 of 2: downloading the stream once to "
                f"count its size (nothing saved to disk).\n"
                f"Counted so far: {human_bytes(counter['bytes'])}\n"
                f"Elapsed: {human_time(time.time() - started)}")

    watcher = asyncio.create_task(_watch())
    try:
        size = await run_blocking(measure_ffmpeg_output,
                                  info["url"], counter)
    except PipeError as exc:
        done["flag"] = True
        await watcher
        await status.edit_text(f"❌ /pipe can't use that link: {exc}")
        return
    finally:
        done["flag"] = True
    await watcher
    if size > config.MAX_FILE_SIZE:
        await status.edit_text(
            f"❌ That stream measures {human_bytes(size)} — over "
            f"Telegram's {human_bytes(config.MAX_FILE_SIZE)} cap "
            f"for bots, so it can't be sent as one file even "
            f"through a pipe.")
        stats.record_job("pipe", ok=False)
        return
    filename = info["filename"]
    await editor.edit(
        f"Piping {filename}\n{human_bytes(size)} — pass 2 of 2: "
        f"streaming to Telegram, no disk…", force=True)
    ffmpeg_file = FfmpegFile(info["url"], size, filename)
    try:
        sent = await _send_piped(client, message, ffmpeg_file,
                                 filename, status, editor)
    finally:
        ffmpeg_file.close()
    if sent is None:
        await status.edit_text("Upload was stopped before it finished.")
        stats.record_job("pipe", ok=False)
        return
    stats.record("uploaded", size)
    # Two ffmpeg passes, so twice the bytes were pulled in.
    stats.record("downloaded", size * 2)
    stats.record_job("pipe", ok=True)
    await status.edit_text(
        f"✅ Piped {filename} ({human_bytes(size)}) — "
        f"HLS/DASH via ffmpeg, nothing touched the server disk.")
    await react(message, "👍")
