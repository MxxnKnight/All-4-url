""" /pipe <link> — stream a direct file straight to Telegram.

No disk: the source is read over HTTP in chunks as Telegram
asks for it (see core/pipe.py for the file-like object and
the WzGram save_file contract it satisfies). The finished
file lands in the chat (or Saved Messages, per /destination)
as a document — no thumbnail, because a thumbnail needs a
file to probe, which a pipe deliberately does not have.

Scope, v1, stated in the command's own usage text so nobody
has to guess:
  * Direct HTTP file links only, one file, known size,
    ≤ Telegram's 2 GB bot cap. Disk-free does not mean
    cap-free: Telegram still refuses >2 GB as one file.
  * NOT playlists (.m3u8/.mpd), NOT yt-dlp/video pages,
    NOT multi-file galleries. Those need a downloader
    (yt-dlp/ffmpeg) that merges segments first — the normal
    uploader, not a chunk pipe.
  * If the source dies mid-upload, there is no partial
    message: the upload fails, the status message says so,
    and the owner retries /pipe (if the link is still
    alive) or sends the link normally (disk path).

Bandwidth: a pipe still counts its Telegram upload once
against the monthly outbound guard (download from the
source is inbound and does not count). It is checked before
starting, like every other job.
"""

import time

from pyrogram import Client, filters

from core import settings, stats
from core.pipe import PipeError, RemoteFile, probe
from core.utils import (JOB_SEMAPHORE, ThrottledEditor, admin_only,
                        human_bytes, progress_text, react, run_blocking)

USAGE = (
    "Usage: `/pipe <direct file link>`\n\n"
    "Streams a direct file straight to Telegram — nothing is "
    "saved on the server, so files that would fill the 2 GB "
    "disk can still land.\n\n"
    "• Direct file links only, one file, ≤ 2 GB\n"
    "• Sent as a document (no video thumbnail — a thumbnail "
    "needs a file to probe)\n"
    "• NOT playlists, video pages, or yt-dlp links — those "
    "need the normal uploader"
)


def _progress_callback(editor: ThrottledEditor):
    started = time.time()

    async def callback(current: int, total: int):
        await editor.edit(progress_text("Piping to Telegram…",
                                        current, total, started))
    return callback


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
            try:
                info = await run_blocking(probe, url)
            except PipeError as exc:
                await status.edit_text(f"❌ /pipe can't use that link: {exc}")
                return
            size, filename = info["size"], info["filename"]
            editor = ThrottledEditor(status)
            await editor.edit(
                f"Piping {filename}\n{human_bytes(size)} — "
                f"no disk, straight to Telegram…", force=True)
            # Destination routing, same as send_files_routed:
            # "saved" goes via the user account to Saved Messages,
            # everything else via the bot to this chat.
            send_client, chat_id = client, message.chat.id
            if settings.get_dest("uploader") == "saved":
                try:
                    from core.userclient import (SessionNotConfigured,
                                                 get_user_client)
                    user = await get_user_client()
                    send_client, chat_id = user, "me"
                except SessionNotConfigured:
                    pass
            remote = RemoteFile(info["url"], size, filename)
            try:
                sent = await send_client.send_document(
                    chat_id, remote, file_name=filename,
                    caption=f"📦 {filename}",
                    progress=_progress_callback(editor))
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
        # A mid-upload source death lands here (RemoteFile raises
        # OSError; WzGram re-raises it after its part retries).
        # No partial message exists in the chat, so the honest
        # recovery is a retry, not a cleanup.
        try:
            await status.edit_text(
                f"❌ Pipe failed: {exc}\n\n"
                f"If the link is still alive, try /pipe again — "
                f"or send the link normally for the disk path.")
        except Exception:
            pass
        stats.record_job("pipe", ok=False)
