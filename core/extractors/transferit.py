"""Custom extractor: transfer.it share links.

Why this exists: transfer.it is MEGA's own file-transfer service
(launched July 2025, "powered by MEGA") — but it is NOT MEGA Drive:
no end-to-end encryption, no MEGA Drive links. The share page is a
JS app shell, so yt-dlp and the scrapers see nothing. MEGA's own
API does the whole job with no login and no key:

  1. POST the f-command to MEGA's API with domain set to
     transferit and x set to the transfer id; it lists the
     transfer's nodes (two API hosts are tried in order — see
     the host list below).
  2. Each node carries its key in the reply; the filename lives in
     the AES-CBC-encrypted attrs blob, decrypted with the node's
     own key (pycryptodome). File keys are 8 words: the attr key
     is the first 4 XORed with the IV and MAC words; folder keys
     are 4 words used as-is.
  3. The file itself is a GET on the same host's download
     endpoint (query: x = transfer id, n = node id, fn =
     filename) with the transfer.it site as Referer — a redirect
     to a fresh userstorage URL serving PLAINTEXT bytes
     (transfer.it decrypts server-side, in memory). Range
     requests work, so the parallel downloader applies.

A transfer.it link has no MEGA Drive equivalent — the transfer ID
is the whole address. A 1DM-extracted userstorage URL is IP-bound
(HEAD 200, GET 403 from another machine), so only the fresh
download URL minted here works; resolve() runs again inside
download() so the URL it uses is always fresh.

Folders download as a whole, up to 100 files. Dead/expired
transfers fail with an honest error.

Verified 2026-11-19: listed one live transfer (one 13.3 GB
mkv, attrs decrypted to the real name), and a 1 MB Range GET on
the fresh download URL answered 206 with a plaintext Matroska
header (EBML magic plus libmatroska strings).
"""

import base64
import json
import os
import random
import re
import struct
from urllib.parse import quote, urlparse

import aiohttp
from Crypto.Cipher import AES

from core.extractors.direct import BROWSER_UA, download_file, safe_filename
from core.extractors.player_scan import FallThrough, HostError

#: MEGA API hosts for the transfer.it calls, in try order. g.api is
#: the web client's host (verified from the sandbox 2026-11-19);
#: bt7 is the host async-mega.py uses for transfer.it.
_API_HOSTS = ("https://g.api.mega.co.nz/cs",
              "https://bt7.api.mega.co.nz/cs")
_REFERER = "https://transfer.it/"
#: Most files a single transfer may download.
_MAX_FILES = 100


def is_site_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if host not in ("transfer.it", "www.transfer.it"):
        return False
    return urlparse(url).path.startswith("/t/")


def _transfer_id(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != "t":
        raise HostError("that transfer.it link has no transfer id in it")
    tid = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", tid):
        raise HostError(
            "that transfer.it link has no usable transfer id in it")
    return tid


def _b64_url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _b64_to_a32(data: str) -> tuple:
    raw = _b64_url_decode(data)
    raw += b"\0" * (-len(raw) % 4)
    return struct.unpack(f">{len(raw) // 4}I", raw)


def _a32_to_bytes(values) -> bytes:
    return struct.pack(f">{len(values)}I", *values)


def _decrypt_name(node: dict) -> str | None:
    """The node's filename from its encrypted attrs, or None.

    Never raises: a node whose attrs won't decrypt keeps a
    fallback name instead of failing the whole transfer.
    """
    try:
        key_b64 = (node.get("k") or "").split(":")[-1]
        full_key = _b64_to_a32(key_b64)
        if node.get("t") == 0:  # file: 8-word key, attr key is XORed
            if len(full_key) != 8:
                return None
            key, iv, mac = (full_key[:4], full_key[4:6], full_key[6:8])
            attr_key = (key[0] ^ iv[0], key[1] ^ iv[1],
                        key[2] ^ mac[0], key[3] ^ mac[1])
        else:  # folder: 4-word key used as-is
            if len(full_key) < 4:
                return None
            attr_key = tuple(full_key[:4])
        blob = _b64_url_decode(node.get("a") or "")
        if not blob or len(blob) % AES.block_size:
            return None
        plain = AES.new(_a32_to_bytes(attr_key), AES.MODE_CBC,
                        b"\0" * AES.block_size).decrypt(blob)
        if not plain.startswith(b'MEGA{"'):
            return None
        text = plain.decode("utf-8", errors="replace").rstrip("\0")
        attrs = json.loads(text[4:text.rfind("}") + 1])
        return attrs.get("n") or None
    except Exception:
        return None


async def _list_nodes(session: aiohttp.ClientSession,
                      tid: str) -> tuple:
    """POST the f-command; returns (working host, node list)."""
    body = [{"a": "f", "c": 1, "r": 1, "xnc": 1}]
    last_exc: Exception | None = None
    for host in _API_HOSTS:
        params = {"id": random.randint(1, 2 ** 31 - 1), "v": 3,
                  "domain": "transferit", "lang": "en", "bc": 1,
                  "x": tid}
        try:
            async with session.post(
                    host, params=params, json=body,
                    headers={"Content-Type": "application/json",
                             "User-Agent": BROWSER_UA}) as resp:
                data = await resp.json(content_type=None)
        except Exception as exc:
            last_exc = exc
            continue
        if not isinstance(data, list) or not data \
                or not isinstance(data[0], dict):
            last_exc = ValueError("unexpected reply")
            continue
        payload = data[0]
        if "e" in payload:
            code = payload["e"]
            if code in (-9, -8):  # ENOENT / EEXPIRED: dead link
                raise HostError(
                    "transfer.it says this transfer doesn't exist "
                    "or has expired")
            if code == -17:  # EOVERQUOTA
                raise HostError(
                    "transfer.it's download quota is used up — "
                    "try again later")
            last_exc = ValueError(f"API error {code}")
            continue
        nodes = payload.get("f")
        if not isinstance(nodes, list):
            last_exc = ValueError("unexpected reply")
            continue
        return host, nodes
    raise FallThrough(f"transfer.it's API didn't answer ({last_exc})")


def _download_url(host: str, tid: str, node_id: str, name: str) -> str:
    return (f"{host}/g?x={tid}&n={node_id}"
            f"&fn={quote(name)}")


def _collect(nodes: list, host: str, tid: str) -> list:
    """Nodes into flat {name, relpath, url, size} rows."""
    folders = {}
    parents = {}
    for node in nodes:
        handle = node.get("h")
        if not handle:
            continue
        parents[handle] = node.get("p") or ""
        if node.get("t") == 1:
            folders[handle] = _decrypt_name(node) or "folder"
    files = []
    for node in nodes:
        if node.get("t") != 0 or not node.get("h"):
            continue
        name = _decrypt_name(node) or "transfer.it file"
        parts: list[str] = []
        parent = node.get("p") or ""
        seen: set[str] = set()
        while parent and parent in folders and parent not in seen:
            seen.add(parent)
            parts.append(folders[parent])
            parent = parents.get(parent, "")
        parts.reverse()
        files.append({"name": name,
                      "relpath": "/".join(parts),
                      "url": _download_url(host, tid, node["h"], name),
                      "size": int(node.get("s") or 0)})
    # One shared top folder (the usual single-folder transfer) adds
    # a pointless nesting level — flatten it away.
    tops = {f["relpath"].split("/", 1)[0] for f in files
            if f["relpath"]}
    if len(tops) == 1 and all(f["relpath"] for f in files):
        for entry in files:
            entry["relpath"] = entry["relpath"].split("/", 1)[1] \
                if "/" in entry["relpath"] else ""
    return files


def _title(nodes: list, files: list) -> str:
    """The transfer's own name: its single root folder, else the
    first file's name."""
    roots = [n for n in nodes
             if n.get("t") == 1 and not (n.get("p") or "")]
    if len(roots) == 1:
        name = _decrypt_name(roots[0])
        if name:
            return name
    return files[0]["name"]


async def resolve(url: str) -> dict:
    """Return {title, media_url, approx_size, is_playlist, files}.

    ``files`` is the full list (one row per file, folders
    flattened); ``media_url`` is the first file's link for callers
    that only need one.
    """
    tid = _transfer_id(url)
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        host, nodes = await _list_nodes(session, tid)
    files = _collect(nodes, host, tid)
    if not files:
        raise HostError("this transfer.it transfer is empty")
    if len(files) > _MAX_FILES:
        raise HostError(
            f"this transfer holds {len(files)} files — more "
            f"than the {_MAX_FILES}-file cap, so the bot won't "
            f"download it whole")
    total = sum(f["size"] for f in files)
    return {"title": _title(nodes, files),
            "media_url": files[0]["url"],
            "approx_size": total, "is_playlist": False,
            "files": files}


async def download(url: str, job_dir: str, progress_cb=None,
                     cancel_check=None) -> list[str]:
    # cancel_check is accepted for the extractor interface but
    # unused here: this module downloads directly, and a direct
    # download aborts when progress_cb raises JobCancelled.
    """Resolve the transfer and download every file; returns paths."""
    info = await resolve(url)
    files = info["files"]
    total = info["approx_size"] or sum(f["size"] for f in files)
    done_base = [0]
    paths: list[str] = []
    for entry in files:
        subdir = os.path.join(job_dir, entry["relpath"]) \
            if entry["relpath"] else job_dir
        os.makedirs(subdir, exist_ok=True)

        async def _progress(done: int, _total: int, _base=done_base[0]):
            if progress_cb:
                await progress_cb(_base + done, total)

        path = await download_file(
            entry["url"], subdir, progress_cb=_progress,
            filename=safe_filename(entry["name"], "transfer.it file"),
            headers={"Referer": _REFERER})
        done_base[0] += entry["size"] or os.path.getsize(path)
        paths.append(path)
    return paths
