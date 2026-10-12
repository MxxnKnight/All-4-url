"""Engine 2 — gallery-dl for image / gallery sites.

Instagram posts and carousels, X multi-image posts, Reddit galleries,
Pinterest, Pixiv and many more: the places where yt-dlp says "no video
formats found". gallery-dl is a CLI, so we run it as a subprocess and
collect whatever lands in the job directory.
"""

import asyncio
import os
import sys

import config
from core.utils import list_files


class GalleryError(Exception):
    pass


async def download(url: str, dest_dir: str) -> list[str]:
    """Download with gallery-dl into dest_dir (-D = exact directory, so
    files land directly inside the job dir). Returns new file paths."""
    before = set(list_files(dest_dir))
    cmd = [sys.executable, "-m", "gallery_dl", "-D", dest_dir,
           "--no-mtime", "--restrict-filenames", "windows"]
    cookies = config.cookies_path()
    if cookies:
        cmd += ["-C", cookies]  # gallery-dl's cookies-file flag
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        raise GalleryError("gallery-dl timed out after 15 minutes")

    new_files = [p for p in list_files(dest_dir) if p not in before]
    if proc.returncode != 0 and not new_files:
        tail = (out or b"").decode(errors="replace").strip().splitlines()
        hint = tail[-1][:300] if tail else f"exit code {proc.returncode}"
        raise GalleryError(hint)
    if not new_files:
        raise GalleryError("gallery-dl found nothing to download")
    return new_files
