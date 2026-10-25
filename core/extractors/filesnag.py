"""FileSnag fallback for Terabox shares — plain HTTP, no browser.

Proven 2026-10-25 against a real Terabox share link (no file
downloaded, resolve only): FileSnag's own page code does one ordinary
POST to its WordPress backend, and a plain client repeating that POST
gets the same direct link back. So the bot can do exactly what the
browser did, with two small requests:

1. GET https://filesnag.com/ and pull the page's `FSN_DL` settings out
   of it. FileSnag ships those settings as a base64 inline script, so
   the nonce lives inside a decoded script, not in the raw HTML.
   The nonce changes regularly — it is fetched fresh on every call
   and never hard-coded, never cached across calls.
2. POST to the settings' `ajaxUrl` (its wp-admin/admin-ajax.php) with
   form fields `action=fsn_resolve`, the fresh `nonce`, the share
   `url`, and `file=0`. The JSON reply lists the share's files with a
   `download_url` each (an `fsn_media` link, good for a few hours).

No browser, no headless Chromium, no sign-in, no payment — that is
the whole point on a 512 MB free host, where a browser would not fit.
FileSnag's links expire after a few hours, so the caller must download
immediately, never store the link. The share link itself is handed to
a third-party site; that is the price of the fallback, and it is why
the bot tries the first-party Terabox cookie route first and only
falls back here when that route fails.

The response shape (verified live): {"ok": true, "files": [{"name",
"size_bytes", "formats": [{"id": "best", "download_url": ...}]}]}.
On failure: {"ok": false, "code": ..., "message": ...}.
"""

import asyncio
import base64
import binascii
import json
import re

import aiohttp

from core.extractors.direct import BROWSER_UA, download_file

HOMEPAGE = "https://filesnag.com/"
DEFAULT_AJAX_URL = "https://filesnag.com/wp-admin/admin-ajax.php"


class FileSnagError(Exception):
    pass


def _fsn_dl_from_text(text: str) -> dict | None:
    """Parse the FSN_DL settings object out of a blob of page/script text.

    The object contains nested dicts (i18n), so a plain ``{...}``
    regex would cut it short — find the opening brace after FSN_DL
    and let the JSON decoder find where the object actually ends.
    Returns None when the blob has no FSN_DL object.
    """
    idx = text.find("FSN_DL")
    if idx < 0:
        return None
    brace = text.find("{", idx)
    if brace < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[brace:])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def extract_fsn_settings(html: str) -> tuple[str, str]:
    """(ajax_url, nonce) from a FileSnag homepage fetch.

    Tries, in order: decoded base64 inline scripts (where FileSnag
    actually ships FSN_DL), then the raw HTML itself. Raises
    FileSnagError when no usable settings are found.
    """
    candidates = []
    for match in re.finditer(r"data:text/javascript;base64,([A-Za-z0-9+/=]+)",
                             html):
        try:
            candidates.append(base64.b64decode(match.group(1)).decode(
                "utf-8", "ignore"))
        except (binascii.Error, ValueError):
            continue
    candidates.append(html)
    for text in candidates:
        settings = _fsn_dl_from_text(text)
        if settings and settings.get("nonce"):
            ajax_url = settings.get("ajaxUrl") or DEFAULT_AJAX_URL
            return ajax_url, str(settings["nonce"])
    raise FileSnagError(
        "could not read FileSnag's page settings — the site may have "
        "changed its page layout")


async def list_files(url: str) -> list[dict]:
    """List a Terabox share via FileSnag: [{name, size, dlink, is_dir}].

    Same shape as terabox.list_files, plus "via": "filesnag", so the
    caller knows which route produced the list (and can tell the
    owner). Download links are FileSnag's own expiring links — use
    them right away.
    """
    headers = {"User-Agent": BROWSER_UA}
    # FileSnag's own page gives its resolve call 60 s, and in practice
    # the call is slow and flaky (observed: ~7 s once, ~31 s another
    # time, and a plain timeout in between) — the resolve backend
    # itself talks to Terabox. So: a generous per-request timeout for
    # the resolve, and one retry on a dropped/slow reply, instead of
    # failing a job the site would have answered a moment later.
    async with aiohttp.ClientSession() as session:
        async with session.get(
                HOMEPAGE, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status >= 400:
                raise FileSnagError(
                    f"FileSnag homepage returned HTTP {resp.status}")
            html = await resp.text()
        ajax_url, nonce = extract_fsn_settings(html)
        form = {"action": "fsn_resolve", "nonce": nonce, "url": url,
                "file": "0"}
        data = None
        last_err = None
        for _attempt in range(2):
            try:
                async with session.post(
                        ajax_url, data=form,
                        headers={**headers,
                                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                        timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status >= 400:
                        raise FileSnagError(
                            f"FileSnag resolve returned HTTP {resp.status}")
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        raise FileSnagError(
                            "FileSnag gave an unreadable answer — the "
                            "site may be down or may have changed its "
                            "reply format")
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_err = exc
                await asyncio.sleep(2)
        if data is None:
            raise FileSnagError(
                "FileSnag did not answer in time "
                f"({type(last_err).__name__ if last_err else 'unknown'})"
                " — the site may be busy; try again in a bit")
    if not isinstance(data, dict) or not data.get("ok"):
        code = data.get("code") if isinstance(data, dict) else None
        message = data.get("message") if isinstance(data, dict) else None
        raise FileSnagError(
            f"FileSnag could not read that link"
            + (f" ({message})" if message else
               f" ({code})" if code else ""))
    files = []
    for item in data.get("files") or []:
        formats = item.get("formats") or []
        best = next((f for f in formats if f.get("id") == "best"),
                    formats[0] if formats else None)
        dlink = (best or {}).get("download_url") or ""
        if not dlink or "fsn_media" not in dlink:
            continue  # no usable download link for this entry
        files.append({
            "name": item.get("name") or data.get("title") or "file",
            "size": int(item.get("size_bytes") or 0),
            "dlink": dlink,
            "is_dir": False,
            "via": "filesnag",
        })
    if not files:
        raise FileSnagError(
            "FileSnag found no downloadable files in that share")
    return files


async def download(url: str, dest_dir: str, index: int = 0,
                   progress_cb=None) -> str:
    """Download one file (by list index) from a Terabox share via FileSnag.

    The fsn_media link carries its own token — no Terabox cookie is
    sent to FileSnag, and no cookie of any kind is needed here.
    """
    files = await list_files(url)
    if index >= len(files):
        index = 0
    chosen = files[index]
    return await download_file(
        chosen["dlink"], dest_dir, progress_cb=progress_cb,
        filename=chosen["name"],
    )
