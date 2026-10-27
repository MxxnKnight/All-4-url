"""Shared helpers for the file menu (file-to-link, /convert, etc.).

A file sent to the bot no longer gets links dumped on it directly.
Instead it gets a menu message, and every button carries the file's
own chat + message id (``fm:<chat>:<msg>:<action>``), so the handler
can re-read the file from Telegram instead of keeping menu state
in memory — menus still work after a bot restart, as long as the
file message itself still exists.

File types, plainly:
  * video content = a Telegram video/animation, or a document whose
    MIME is video/* or whose extension is a video extension (an mp4
    sent as a document is still a video, just wearing a document
    coat). Only video content gets Convert / Screenshots / Sample.
  * zip = a document whose name ends in .zip. Only zips get Unzip.
  * everything else gets Links + MediaInfo + Close only — converting
    a PDF to a video, or screenshotting a PDF, would only fail.
"""

import os

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.utils import VIDEO_EXTS, human_bytes


def file_info(message):
    """(name, size, mime, kind, is_video, is_zip) for a media message."""
    kind = None
    media = None
    for k in ("video", "document", "audio", "voice", "animation"):
        m = getattr(message, k, None)
        if m is not None:
            kind, media = k, m
            break
    if media is None and message.photo is not None:
        return ("photo.jpg", message.photo.file_size or 0,
                "image/jpeg", "photo", False, False)
    if media is None:
        return (None, 0, "", "", False, False)
    name = getattr(media, "file_name", None) or kind
    size = getattr(media, "file_size", 0) or 0
    mime = getattr(media, "mime_type", "") or ""
    ext = os.path.splitext(name)[1].lower()
    is_video = kind in ("video", "animation") \
        or mime.startswith("video/") or ext in VIDEO_EXTS
    is_zip = name.lower().endswith(".zip")
    return (name, size, mime, kind, is_video, is_zip)


def menu_text(name, size):
    return (f"**{name}** ({human_bytes(size)})\n\n"
            f"What do you want to do with this file?")


def main_menu_markup(chat_id, msg_id, is_video, is_zip):
    """The file menu itself. MediaInfo reuses the existing
    ``mi:<chat>:<msg>`` callback, so that button needs no new handler."""
    base = f"fm:{chat_id}:{msg_id}"
    rows = [
        [InlineKeyboardButton("🔗 Generate links",
                              callback_data=f"{base}:links"),
         InlineKeyboardButton("📊 MediaInfo",
                              callback_data=f"mi:{chat_id}:{msg_id}")],
    ]
    if is_video:
        rows.append([
            InlineKeyboardButton("🔄 Convert",
                                 callback_data=f"{base}:conv"),
            InlineKeyboardButton("📸 Screenshots",
                                 callback_data=f"{base}:shot"),
            InlineKeyboardButton("🎬 Sample video",
                                 callback_data=f"{base}:samp")])
    if is_zip:
        rows.append([InlineKeyboardButton("📂 Unzip & send contents",
                                          callback_data=f"{base}:unzip")])
    rows.append([InlineKeyboardButton("❌ Close",
                                      callback_data=f"{base}:close")])
    return InlineKeyboardMarkup(rows)


def convert_menu_markup(chat_id, msg_id, kind):
    """Convert sub-menu. Only the *other* type is offered — converting
    a video to a video is a no-op, and hiding it avoids confusion."""
    base = f"fm:{chat_id}:{msg_id}"
    cv = f"cv:{chat_id}:{msg_id}"
    if kind == "video":
        text = "This file is currently a **video**. Convert it to a document?"
        rows = [[InlineKeyboardButton("📄 Convert to document",
                                      callback_data=f"{cv}:doc")]]
    elif kind == "animation":
        text = ("This file is currently an **animation**. "
                "Convert it to a video or a document?")
        rows = [[InlineKeyboardButton("🎬 Convert to video",
                                      callback_data=f"{cv}:vid"),
                 InlineKeyboardButton("📄 Convert to document",
                                      callback_data=f"{cv}:doc")]]
    else:
        text = ("This file is currently a **document**. "
                "Convert it to a video? (Only works if the file "
                "itself is a video, e.g. an mp4 sent as a document.)")
        rows = [[InlineKeyboardButton("🎬 Convert to video",
                                      callback_data=f"{cv}:vid")]]
    rows.append([InlineKeyboardButton("⬅️ Back",
                                      callback_data=f"{base}:back"),
                 InlineKeyboardButton("❌ Close",
                                      callback_data=f"{base}:close")])
    return text, InlineKeyboardMarkup(rows)


def screenshot_menu_markup(chat_id, msg_id):
    base = f"fm:{chat_id}:{msg_id}"
    fs = f"fs:{chat_id}:{msg_id}"
    rows = [[InlineKeyboardButton(f"{n} screenshot{'s' if n > 1 else ''}",
                                  callback_data=f"{fs}:{n}")
             for n in (1, 3)],
            [InlineKeyboardButton("5 screenshots",
                                  callback_data=f"{fs}:5"),
             InlineKeyboardButton("10 screenshots",
                                  callback_data=f"{fs}:10")],
            [InlineKeyboardButton("⬅️ Back",
                                  callback_data=f"{base}:back"),
             InlineKeyboardButton("❌ Close",
                                  callback_data=f"{base}:close")]]
    return ("How many screenshots? They are taken evenly spaced "
            "through the video.", InlineKeyboardMarkup(rows))


def sample_menu_markup(chat_id, msg_id):
    base = f"fm:{chat_id}:{msg_id}"
    fp = f"fp:{chat_id}:{msg_id}"
    rows = [[InlineKeyboardButton("15 seconds",
                                  callback_data=f"{fp}:15"),
             InlineKeyboardButton("30 seconds",
                                  callback_data=f"{fp}:30"),
             InlineKeyboardButton("60 seconds",
                                  callback_data=f"{fp}:60")],
            [InlineKeyboardButton("⬅️ Back",
                                  callback_data=f"{base}:back"),
             InlineKeyboardButton("❌ Close",
                                  callback_data=f"{base}:close")]]
    return ("How long should the sample be? It is cut from the "
            "middle of the video.", InlineKeyboardMarkup(rows))


def back_close_markup(chat_id, msg_id):
    base = f"fm:{chat_id}:{msg_id}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back to file menu",
                             callback_data=f"{base}:back"),
        InlineKeyboardButton("❌ Close",
                             callback_data=f"{base}:close")]])
