"""MediaInfo by partial probe — download only the first bits of a file.

Downloading a whole 2 GB file just to describe it would be madness on a
free-tier box, and usually unnecessary: container headers and stream
metadata live at the start of the file (and, for MP4/MOV, sometimes in a
`moov` atom at the very end). So we stream at most HEAD_CHUNKS chunks
(~1 MiB each in this library's stream_media), run the MediaInfo CLI over
that head (ffprobe as fallback), and — for MP4/MOV/MKV files whose head
yielded no stream info — retry once with the last chunks appended.
Total data touched is capped at ~16 MiB no matter how big the file is.
"""

import json
import math
import mimetypes
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from core.utils import human_bytes, human_time, make_job_dir, cleanup_dir, run_blocking

CHUNK_SIZE = 1024 * 1024  # stream_media counts limit/offset in ~1 MiB chunks
HEAD_CHUNKS = 12
TAIL_CHUNKS = 4
TAIL_EXTS = {".mp4", ".mov", ".m4v", ".mkv"}


class ProbeError(Exception):
    """The probe could not produce any usable metadata."""


@dataclass
class MediaInfoResult:
    file_name: str
    file_size: int
    probed_bytes: int
    text: str                      # full report, every field of every track
    summary: dict = field(default_factory=dict)
    tool: str = ""                 # "mediainfo" | "ffprobe"


# ------------------------------------------------------------------ helpers

def _to_int(value):
    try:
        return int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(str(value).replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _bitrate_text(bps) -> str:
    bps = _to_int(bps)
    if not bps:
        return ""
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} kbps"
    return f"{bps} bps"


def _media_of(message):
    """(media_object, kind) for the first downloadable media on a message."""
    for kind in ("document", "video", "audio", "animation", "voice"):
        media = getattr(message, kind, None)
        if media is not None:
            return media, kind
    return None, None


def _extension(media, kind: str) -> str:
    name = getattr(media, "file_name", None) or ""
    ext = os.path.splitext(name)[1].lower()
    if ext:
        return ext
    mime = getattr(media, "mime_type", "") or ""
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return ".mkv" if guessed == ".mk3d" else guessed
    return {"voice": ".ogg", "animation": ".mp4"}.get(kind, ".bin")


# ------------------------------------------------------- report construction

def _flatten(track: dict) -> list[tuple[str, str]]:
    """All fields of one track as (key, value) lines, one level deep."""
    lines = []
    for key, value in track.items():
        if key == "@type":
            continue
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                lines.append((sub_key, str(sub_value)))
        elif isinstance(value, list):
            lines.append((key, ", ".join(str(v) for v in value)))
        else:
            lines.append((key, str(value)))
    return lines


def _report_from_tracks(tracks: list[tuple[str, dict]]) -> str:
    out = []
    counters: dict[str, int] = {}
    total = {}
    for kind, _ in tracks:
        total[kind] = total.get(kind, 0) + 1
    for kind, track in tracks:
        counters[kind] = counters.get(kind, 0) + 1
        suffix = f" #{counters[kind]}" if total[kind] > 1 else ""
        out.append(f"===== {kind}{suffix} =====")
        for key, value in _flatten(track):
            out.append(f"{key}: {value}")
        out.append("")
    return "\n".join(out).strip() + "\n"


def _score(summary: dict) -> int:
    score = 0
    if summary.get("video"):
        score += 3
    score += len(summary.get("audio") or [])
    if summary.get("format"):
        score += 1
    return score


# ------------------------------------------------------------- mediainfo CLI

def _summary_from_mediainfo(tracks_raw: list[dict]) -> dict:
    by_type: dict[str, list[dict]] = {}
    for track in tracks_raw:
        by_type.setdefault(track.get("@type", ""), []).append(track)
    general = (by_type.get("General") or [{}])[0]
    summary: dict = {
        "format": general.get("Format", ""),
        "duration": _to_float(general.get("Duration")),
        "overall_bitrate": _to_int(general.get("OverallBitRate")),
        "video": None,
        "audio": [],
        "subtitles": [],
    }
    for track in by_type.get("Video", []):
        summary["video"] = {
            "codec": track.get("Format", ""),
            "width": _to_int(track.get("Width")),
            "height": _to_int(track.get("Height")),
            "fps": _to_float(track.get("FrameRate")),
            "bitrate": _to_int(track.get("BitRate")),
        }
        break
    for track in by_type.get("Audio", []):
        summary["audio"].append({
            "codec": track.get("Format", ""),
            "channels": _to_int(track.get("Channels")),
            "language": track.get("Language", ""),
            "bitrate": _to_int(track.get("BitRate")),
        })
    for track in by_type.get("Text", []):
        summary["subtitles"].append({
            "codec": track.get("Format", ""),
            "language": track.get("Language", "") or track.get("Title", ""),
        })
    return summary


def _analyse_mediainfo(path: str):
    try:
        out = subprocess.run(
            ["mediainfo", "--Output=JSON", path],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return None
    tracks_raw = (data.get("media") or {}).get("track") or []
    if not tracks_raw:
        return None
    tracks = [(t.get("@type", "Other"), t) for t in tracks_raw]
    summary = _summary_from_mediainfo(tracks_raw)
    return _report_from_tracks(tracks), summary, _score(summary)


# ------------------------------------------------------------------ ffprobe

def _analyse_ffprobe(path: str):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return None
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    if not fmt and not streams:
        return None

    tracks: list[tuple[str, dict]] = [("General", {
        "Format": fmt.get("format_long_name") or fmt.get("format_name", ""),
        "Duration": fmt.get("duration", ""),
        "Size": fmt.get("size", ""),
        "BitRate": fmt.get("bit_rate", ""),
        **{f"tag.{k}": v for k, v in (fmt.get("tags") or {}).items()},
    })]
    summary: dict = {
        "format": fmt.get("format_name", ""),
        "duration": _to_float(fmt.get("duration")),
        "overall_bitrate": _to_int(fmt.get("bit_rate")),
        "video": None,
        "audio": [],
        "subtitles": [],
    }
    labels = {"video": "Video", "audio": "Audio", "subtitle": "Text"}
    for stream in streams:
        ctype = stream.get("codec_type", "")
        flat = {k: v for k, v in stream.items() if k != "tags"}
        flat.update({f"tag.{k}": v for k, v in (stream.get("tags") or {}).items()})
        tracks.append((labels.get(ctype, "Other"), flat))
        tags = stream.get("tags") or {}
        if ctype == "video" and summary["video"] is None:
            fps = None
            rate = stream.get("avg_frame_rate") or ""
            if "/" in rate:
                num, _, den = rate.partition("/")
                if _to_float(den):
                    fps = (_to_float(num) or 0) / _to_float(den)
            summary["video"] = {
                "codec": stream.get("codec_name", ""),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "fps": fps,
                "bitrate": _to_int(stream.get("bit_rate")),
            }
        elif ctype == "audio":
            summary["audio"].append({
                "codec": stream.get("codec_name", ""),
                "channels": stream.get("channels"),
                "language": tags.get("language", ""),
                "bitrate": _to_int(stream.get("bit_rate")),
            })
        elif ctype == "subtitle":
            summary["subtitles"].append({
                "codec": stream.get("codec_name", ""),
                "language": tags.get("language", "") or tags.get("title", ""),
            })
    return _report_from_tracks(tracks), summary, _score(summary)


def _analyse(path: str):
    """Best available analysis of a local probe file, or None."""
    candidates = []
    if shutil.which("mediainfo"):
        result = _analyse_mediainfo(path)
        if result:
            candidates.append((*result, "mediainfo"))
    if shutil.which("ffprobe"):
        result = _analyse_ffprobe(path)
        if result:
            candidates.append((*result, "ffprobe"))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[2], reverse=True)
    text, summary, score, tool = candidates[0]
    return text, summary, score, tool


# ---------------------------------------------------------------- the probe

async def _write_head(client, message, path: str, file_size: int) -> int:
    written = 0
    cap = HEAD_CHUNKS * CHUNK_SIZE
    with open(path, "wb") as handle:
        async for chunk in client.stream_media(message, limit=HEAD_CHUNKS):
            if not chunk:
                continue
            handle.write(chunk)
            written += len(chunk)
            if written >= cap or (file_size and written >= file_size):
                break
    return written


async def probe_message(client, message) -> MediaInfoResult:
    """Probe a message's media by streaming only its first (and last) bits."""
    media, kind = _media_of(message)
    if media is None:
        raise ProbeError("that message has no media file I can probe")
    file_name = getattr(media, "file_name", None) or f"{kind} file"
    file_size = int(getattr(media, "file_size", 0) or 0)
    ext = _extension(media, kind)

    job_dir = make_job_dir()
    try:
        head_path = os.path.join(job_dir, "probe" + ext)
        written = await _write_head(client, message, head_path, file_size)
        if written == 0:
            raise ProbeError("Telegram returned no data for that file")
        probed = written

        best = await run_blocking(_analyse, head_path)
        has_streams = bool(best and (best[1].get("video") or best[1].get("audio")))

        # moov-at-end rescue: head gave no stream info, the file is a
        # container that may keep metadata at the tail, and there *is*
        # more of the file than the head we already have.
        if (not has_streams and ext in TAIL_EXTS and file_size > written):
            total_chunks = math.ceil(file_size / CHUNK_SIZE)
            offset = max(0, total_chunks - TAIL_CHUNKS)
            tail_path = os.path.join(job_dir, "probe_tail" + ext)
            shutil.copyfile(head_path, tail_path)
            tail_written = 0
            cap = TAIL_CHUNKS * CHUNK_SIZE
            with open(tail_path, "ab") as handle:
                async for chunk in client.stream_media(
                        message, limit=TAIL_CHUNKS, offset=offset):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    tail_written += len(chunk)
                    if tail_written >= cap:
                        break
            probed += tail_written
            alt = await run_blocking(_analyse, tail_path)
            if alt and (best is None or alt[2] > best[2]):
                best = alt

        if best is None:
            raise ProbeError(
                "neither mediainfo nor ffprobe could read any metadata "
                "from the start of this file — it may be corrupt, an "
                "unsupported container, or split across multiple files")
        text, summary, _score_value, tool = best
        header = (f"MediaInfo for {file_name} "
                  f"({human_bytes(file_size)}, probed {human_bytes(probed)} "
                  f"via {tool})\n\n")
        return MediaInfoResult(
            file_name=file_name, file_size=file_size, probed_bytes=probed,
            text=header + text, summary=summary, tool=tool)
    finally:
        cleanup_dir(job_dir)


def report_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a probe report into (header, [(section_title, body), ...]).

    Sections are the `===== Kind =====` blocks built by
    _report_from_tracks; everything before the first block is the
    header line. Used to build a sectioned Telegraph page.
    """
    header_lines: list[str] = []
    sections: list[tuple[str, str]] = []
    title: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("===== ") and line.endswith(" ====="):
            if title is not None:
                sections.append((title, "\n".join(body).strip()))
            title = line[len("===== "):-len(" =====")]
            body = []
        elif title is None:
            if line.strip():
                header_lines.append(line.strip())
        else:
            body.append(line)
    if title is not None:
        sections.append((title, "\n".join(body).strip()))
    return " ".join(header_lines), sections


def format_summary(result: MediaInfoResult) -> str:
    """Compact in-chat summary (markdown) for a probe result."""
    summary = result.summary or {}
    lines = [f"📊 **MediaInfo** — {result.file_name} "
             f"({human_bytes(result.file_size)})"]
    meta = []
    if summary.get("format"):
        meta.append(f"Format: {summary['format']}")
    if summary.get("duration"):
        meta.append(f"Duration: {human_time(summary['duration'])}")
    bitrate = _bitrate_text(summary.get("overall_bitrate"))
    if bitrate:
        meta.append(f"Bitrate: {bitrate}")
    if meta:
        lines.append(" · ".join(meta))

    video = summary.get("video")
    if video:
        parts = [video.get("codec") or "?"]
        if video.get("width") and video.get("height"):
            parts.append(f"{video['width']}x{video['height']}")
        if video.get("fps"):
            parts.append(f"{video['fps']:.2f} fps")
        br = _bitrate_text(video.get("bitrate"))
        if br:
            parts.append(br)
        lines.append("🎬 Video: " + " · ".join(parts))

    audios = summary.get("audio") or []
    for i, audio in enumerate(audios):
        parts = [audio.get("codec") or "?"]
        if audio.get("channels"):
            parts.append(f"{audio['channels']} ch")
        if audio.get("language"):
            parts.append(audio["language"])
        br = _bitrate_text(audio.get("bitrate"))
        if br:
            parts.append(br)
        label = f"🔊 Audio {i + 1}" if len(audios) > 1 else "🔊 Audio"
        lines.append(f"{label}: " + " · ".join(parts))

    subs = summary.get("subtitles") or []
    if subs:
        langs = [s.get("language") or s.get("codec") or "?" for s in subs]
        lines.append("💬 Subtitles: " + ", ".join(langs))

    lines.append(f"_Probed from just {human_bytes(result.probed_bytes)} "
                 f"of the file — no full download._")
    return "\n".join(lines)
