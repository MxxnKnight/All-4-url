"""Engine 2 — gallery-dl for image / gallery sites.

Instagram posts and carousels, X multi-image posts, Reddit galleries,
Pinterest, Pixiv and many more: the places where yt-dlp says "no video
formats found". gallery-dl is a CLI, so we run it as a subprocess and
collect whatever lands in the job directory.
"""

import asyncio
import os
import sys
from urllib.parse import urlparse

import config
from core import stats
from core.cookies import jar_for_url
from core.jobs import JobCancelled
from core.utils import list_files


class GalleryError(Exception):
    pass


def _build_cmd(url: str, dest_dir: str) -> list[str]:
    """The gallery-dl invocation for `url`.

    Fansly (v1.51.19): gallery-dl's Fansly extractor needs its API
    token — a localStorage value, not a cookie, so no cookies.txt
    can carry it. When FANSLY_AUTH_TOKEN is set, pass it as
    gallery-dl's `extractor.fansly.token` option (verified: the
    token reaches the API as the authorization header; a fake one
    gets a 401 from apiv3.fansly.com). Without it only public
    previews download.
    """
    cmd = [sys.executable, "-m", "gallery_dl", "-D", dest_dir,
           "--no-mtime", "--restrict-filenames", "windows"]
    cookies = jar_for_url(url)
    if cookies:
        cmd += ["-C", cookies]  # gallery-dl's cookies-file flag
    host = (urlparse(url or "").netloc or "").lower()
    if host in ("fansly.com", "www.fansly.com") and \
            (config.FANSLY_AUTH_TOKEN or "").strip():
        cmd += ["-o", "extractor.fansly.token="
                + (config.FANSLY_AUTH_TOKEN or "").strip()]
    cmd.append(url)
    return cmd


async def download(url: str, dest_dir: str,
                   cancel_check=None) -> list[str]:
    """Download with gallery-dl into dest_dir (-D = exact directory, so
    files land directly inside the job dir). Returns new file paths.
    `cancel_check`, when given, is polled once a second — a True
    answer kills the gallery-dl process and raises JobCancelled."""
    before = set(list_files(dest_dir))
    cmd = _build_cmd(url, dest_dir)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _watch_cancel():
        while proc.returncode is None:
            if cancel_check is not None and cancel_check():
                proc.kill()
                return
            await asyncio.sleep(1)

    watch_task = (asyncio.create_task(_watch_cancel())
                  if cancel_check is not None else None)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        raise GalleryError("gallery-dl timed out after 15 minutes")
    finally:
        if watch_task is not None:
            watch_task.cancel()
    if cancel_check is not None and cancel_check():
        raise JobCancelled()

    new_files = [p for p in list_files(dest_dir) if p not in before]
    if proc.returncode != 0 and not new_files:
        tail = (out or b"").decode(errors="replace").strip().splitlines()
        hint = tail[-1][:300] if tail else f"exit code {proc.returncode}"
        raise GalleryError(hint)
    if not new_files:
        raise GalleryError("gallery-dl found nothing to download")
    stats.record_paths("downloaded", new_files)
    return new_files
