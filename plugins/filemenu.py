"""The file menu itself: ``fm:<chat>:<msg>:<action>``.

Actions:
  links — generate the stream/download links (the old file-to-link
          output, now behind a button instead of dumped directly)
  conv  — show the Convert sub-menu (handled by plugins/convert.py
          once the user picks a type)
  shot  — show the Screenshot count sub-menu
  samp  — show the Sample length sub-menu
  unzip — unzip a zip file, one by one (same engine as /unzip)
  back  — return to the main menu
  close — delete the menu message (the file itself is untouched,
          and any links already generated keep working until expiry)

MediaInfo is NOT handled here: the menu's 📊 button uses the
existing ``mi:<chat>:<msg>`` callback, so plugins/mediainfo.py
handles it and sends its report as a separate message.
"""

import os

from pyrogram import Client, filters

from core import filemenu, stats, unzip
from core.linktoken import make_token
from core.sender import send_files_routed
from core.utils import (admin_only, cleanup_dir, human_bytes, is_admin_id,
                        make_job_dir)
import config


def _parse(query):
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[0] != "fm":
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3]
    except ValueError:
        return None


async def _main_menu(client, query, chat_id, msg_id):
    try:
        target = await client.get_messages(chat_id, msg_id)
    except Exception:
        target = None
    if target is None or getattr(target, "empty", False):
        await query.answer("That file message is gone.",
                           show_alert=True)
        return
    name, size, _mime, _kind, is_video, is_zip = filemenu.file_info(target)
    await query.message.edit_text(
        filemenu.menu_text(name, size),
        reply_markup=filemenu.main_menu_markup(
            chat_id, msg_id, is_video, is_zip))


@Client.on_callback_query(filters.regex(r"^fm:"))
async def filemenu_callback(client: Client, query):
    if not is_admin_id(query.from_user.id if query.from_user else None):
        await query.answer()
        return
    parsed = _parse(query)
    if parsed is None:
        await query.answer("Stale button.")
        return
    chat_id, msg_id, action = parsed
    if action == "close":
        await query.answer("Closed.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return
    if action == "back":
        await query.answer()
        await _main_menu(client, query, chat_id, msg_id)
        return
    # All other actions need the file message to still exist.
    try:
        target = await client.get_messages(chat_id, msg_id)
    except Exception:
        target = None
    if target is None or getattr(target, "empty", False):
        await query.answer("That file message is gone — links die with it.",
                           show_alert=True)
        return
    name, size, _mime, kind, is_video, is_zip = filemenu.file_info(target)
    if action == "conv":
        if not is_video:
            await query.answer("Convert only works for videos.",
                               show_alert=True)
            return
        await query.answer()
        text, markup = filemenu.convert_menu_markup(chat_id, msg_id, kind)
        await query.message.edit_text(
            f"🔄 **{name}** ({human_bytes(size)})\n\n{text}",
            reply_markup=markup)
        return
    if action == "shot":
        if not is_video:
            await query.answer("Screenshots only work for videos.",
                               show_alert=True)
            return
        await query.answer()
        text, markup = filemenu.screenshot_menu_markup(chat_id, msg_id)
        await query.message.edit_text(
            f"📸 **{name}**\n\n{text}", reply_markup=markup)
        return
    if action == "samp":
        if not is_video:
            await query.answer("Samples only work for videos.",
                               show_alert=True)
            return
        await query.answer()
        text, markup = filemenu.sample_menu_markup(chat_id, msg_id)
        await query.message.edit_text(
            f"🎬 **{name}**\n\n{text}", reply_markup=markup)
        return
    if action == "links":
        if not config.BASE_URL:
            await query.answer("BASE_URL is not set on the server.",
                               show_alert=True)
            return
        await query.answer("Generating links…")
        _n, _s, mime, _k, _v, _z = filemenu.file_info(target)
        token = make_token(chat_id, msg_id, name or "file", size, mime)
        download_url = f"{config.BASE_URL}/d/{token}"
        stream_url = f"{config.BASE_URL}/stream/{token}"
        from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        await query.message.edit_text(
            f"**{name}** ({human_bytes(size)})\n\n"
            f"⬇️ Download: {download_url}\n"
            f"▶️ Stream: {stream_url}\n\n"
            f"Links expire in {config.LINK_EXPIRY_HOURS} hours.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Download", url=download_url),
                 InlineKeyboardButton("▶️ Stream", url=stream_url)],
                [InlineKeyboardButton("📊 MediaInfo",
                                      callback_data=f"mi:{chat_id}:{msg_id}")],
                [InlineKeyboardButton("⬅️ Back to file menu",
                                      callback_data=f"fm:{chat_id}:{msg_id}:back"),
                 InlineKeyboardButton("❌ Close",
                                      callback_data=f"fm:{chat_id}:{msg_id}:close")]]))
        stats.record_job("file-to-link", ok=True)
        return
    if action == "unzip":
        if not is_zip:
            await query.answer("That file is not a zip.",
                               show_alert=True)
            return
        await query.answer("Unzipping…")
        status = query.message
        job_dir = make_job_dir()
        try:
            try:
                info = None
                zip_path = await client.download_media(
                    target, file_name=os.path.join(job_dir, "input.zip"))
                if zip_path is None or not os.path.exists(zip_path):
                    await status.edit_text(
                        "❌ Couldn't download that zip back.",
                        reply_markup=filemenu.back_close_markup(
                            chat_id, msg_id))
                    return
                if not unzip.is_zip_file(zip_path):
                    await status.edit_text(
                        "❌ That file isn't actually a zip.",
                        reply_markup=filemenu.back_close_markup(
                            chat_id, msg_id))
                    return
                info = unzip.zip_info(zip_path)
                unzip.check_disk_room(zip_path, info["largest"])
            except unzip.ZipTooBig as exc:
                await status.edit_text(
                    f"📦 Can't unzip this one: {exc}",
                    reply_markup=filemenu.back_close_markup(
                        chat_id, msg_id))
                return
            except unzip.ZipBad as exc:
                await status.edit_text(
                    f"❌ {exc}",
                    reply_markup=filemenu.back_close_markup(
                        chat_id, msg_id))
                return
            skipped = list(info["skipped"]) + info["encrypted"] \
                + info["too_big"]
            failed: list[str] = []
            sent_count = 0
            sent_bytes = 0
            bandwidth_stopped = False
            gen = unzip.iter_extracted(
                zip_path, os.path.join(job_dir, "extracted"), skipped)
            try:
                for path in gen:
                    if stats.bandwidth_blocked():
                        bandwidth_stopped = True
                        break
                    try:
                        sz = os.path.getsize(path)
                    except OSError:
                        sz = 0
                    try:
                        await send_files_routed(
                            client, status.chat.id, [path],
                            "uploader", caption=f"Unzipped from {name}",
                            status_msg=status)
                        sent_count += 1
                        sent_bytes += sz
                    except Exception as exc:
                        failed.append(f"{os.path.basename(path)} ({exc})")
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            finally:
                gen.close()
            summary = (f"✅ Unzipped {sent_count} file(s) "
                       f"({human_bytes(sent_bytes)}) from {name}.")
            if bandwidth_stopped:
                summary += ("\n⚠️ Stopped early: monthly bandwidth "
                            "cap reached.")
            if failed:
                summary += "\n⚠️ Failed: " + ", ".join(failed[:5])
            if skipped:
                summary += "\n⚠️ Skipped: " + ", ".join(skipped[:10])
            await status.edit_text(
                summary,
                reply_markup=filemenu.back_close_markup(chat_id, msg_id))
        except Exception as exc:
            try:
                await status.edit_text(
                    f"❌ Unzip failed: {exc}",
                    reply_markup=filemenu.back_close_markup(chat_id, msg_id))
            except Exception:
                pass
        finally:
            cleanup_dir(job_dir)
        return
    await query.answer("Stale button.")
