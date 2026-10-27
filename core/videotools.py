"""Screenshot + sample helpers for Telegram video files.

Both features need the video on disk first (ffmpeg works on a
file, not on a Telegram message). That is the honest limit of
this whole feature: a video bigger than free disk minus headroom
cannot be screenshotted or sampled on this 2 GB server — the
bot refuses it plainly instead of filling the disk and crashing
mid-job. Small and medium videos (the usual case) work fine.

Screenshot positions are evenly spaced through the video
(1 screenshot = middle, 3 = quarters, …). Sample clips are cut
from the middle, by copy first (fast, same quality) and by
re-encode second (slower, but works for containers/codecs
that copy cannot cut cleanly).
"""

import json
import os
import shutil
import subprocess

HEADROOM_BYTES = 256 * 1024 * 1024


def _ffprobe(path: str) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout or "{}")
    except Exception:
        return {}


def duration_of(path: str) -> float:
    try:
        data = _ffprobe(path)
        return float(data.get("format", {}).get("duration") or 0)
    except Exception:
        return 0.0


def check_disk_for(size: int, job_dir: str) -> None:
    """Raise ValueError if the video cannot fit on disk."""
    try:
        free = shutil.disk_usage(job_dir).free
    except Exception:
        return
    if size + HEADROOM_BYTES > free:
        from core.utils import human_bytes
        raise ValueError(
            f"this video is {human_bytes(size)}, but the server "
            f"only has {human_bytes(free)} free — it cannot be "
            f"downloaded here for screenshots/samples")


def make_screenshots(path: str, duration: float, count: int,
                     out_dir: str) -> list[str]:
    """Evenly spaced JPEG screenshots. Returns paths that exist."""
    os.makedirs(out_dir, exist_ok=True)
    shots: list[str] = []
    for i in range(count):
        if duration > 0:
            ts = duration * (i + 0.5) / count
        else:
            ts = 1.0 + i * 10.0
        out = os.path.join(out_dir, f"shot_{i + 1:02d}.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", path,
                 "-frames:v", "1", "-q:v", "3", out],
                capture_output=True, timeout=60)
        except Exception:
            continue
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            shots.append(out)
    return shots


def make_sample(path: str, duration: float, length: int,
                out_path: str) -> str:
    """A sample clip from the middle. Copy first, re-encode fallback."""
    start = max(0.0, duration / 2 - length / 2) if duration > length else 0.0
    # Copy: fast, same quality, but only cuts at keyframes.
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-i", path,
             "-t", str(length), "-c", "copy", out_path],
            capture_output=True, timeout=120)
    except Exception:
        pass
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0 \
            and duration_of(out_path) > 0:
        return out_path
    # Re-encode: slower, but cuts exactly and always lands as mp4.
    try:
        os.unlink(out_path)
    except OSError:
        pass
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(start), "-i", path,
         "-t", str(length), "-c:v", "libx264", "-preset", "veryfast",
         "-c:a", "aac", out_path],
        capture_output=True, timeout=300)
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    raise ValueError("ffmpeg could not cut a sample from this video")
