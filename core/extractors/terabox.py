"""Custom extractor — Terabox (yt-dlp upstream refuses to support it).

Original implementation of the publicly documented share flow:

1. Pull the share id (`surl`) out of any of the many Terabox mirror
   domains.
2. Fetch the share page and scrape the `jsToken` value out of it.
3. Call the share/list API with the account's `ndus` cookie to list the
   files in the share, including a direct download link (`dlink`) each.
4. Download the chosen file's dlink with a browser user-agent + cookie.

Terabox changes this flow from time to time; when it breaks, the API
step is the part to re-check. A valid TERABOX_NDUS cookie is required.
"""

import re
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

import config
from core.extractors.direct import BROWSER_UA, download_file

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "1024tera.com", "teraboxapp.com",
    "mirrobox.com", "nephobox.com", "terasharelink.com", "4funbox.com",
    "momerybox.com", "freemb.com", "mibox.fun", "teraboxlink.com",
    "terasharefile.com", "terafileshare.com",
)

API_ROOT = "https://www.terabox.com"


class TeraboxError(Exception):
    pass


def is_terabox_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in TERABOX_DOMAINS)


def extract_surl(url: str) -> str | None:
    """The share id, from /s/1<id> paths or ?surl=1<id> queries.

    Share URLs carry a fixed leading "1" marker; the share/list API
    expects the id without it, so strip it in both shapes.
    """
    parsed = urlparse(url)
    match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    surl = match.group(1) if match else (parse_qs(parsed.query).get("surl") or [None])[0]
    if surl and surl.startswith("1") and len(surl) > 8:
        return surl[1:]
    return surl


def _cookies() -> dict:
    if not config.TERABOX_NDUS:
        raise TeraboxError(
            "TERABOX_NDUS is not set. Terabox needs the `ndus` cookie of a "
            "logged-in Terabox account — see the README ('Terabox cookie') "
            "for the 1-minute browser steps, then add it as an env var."
        )
    return {"ndus": config.TERABOX_NDUS}


async def list_files(url: str) -> list[dict]:
    """List the files in a Terabox share: [{name, size, dlink, is_dir}]."""
    cookies = _cookies()
    surl = extract_surl(url)
    if not surl:
        raise TeraboxError("could not find a share id in that Terabox link")

    headers = {"User-Agent": BROWSER_UA, "Referer": API_ROOT + "/"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, cookies=cookies) as session:
        # 1) share page -> jsToken
        share_url = f"{API_ROOT}/s/1{surl}" if not url.startswith(API_ROOT) else url
        async with session.get(share_url, headers=headers) as resp:
            html = await resp.text()
        token_match = re.search(r"jsToken.*?([A-Za-z0-9._~-]{20,})", html, re.S)
        quoted = re.search(r'"jsToken"\s*:\s*"([^"]+)"', html)
        js_token = (quoted.group(1) if quoted else
                    token_match.group(1) if token_match else None)
        if not js_token:
            raise TeraboxError(
                "could not read jsToken from the share page — Terabox may "
                "have changed its page layout, or the ndus cookie expired")

        # 2) share/list API
        api = f"{API_ROOT}/api/share/list"
        params = {
            "app_id": "250528", "web": "1", "channel": "dubox",
            "clienttype": "0", "jsToken": js_token, "shorturl": surl,
            "root": "1",
        }
        async with session.get(api, params=params, headers=headers) as resp:
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or data.get("errno") not in (0, None) \
                and not data.get("list"):
            raise TeraboxError(
                f"Terabox list API error (errno={data.get('errno') if isinstance(data, dict) else '?'}) "
                "— the link may be dead or the cookie may have expired")

        files = []
        for item in data.get("list") or []:
            files.append({
                "name": unquote(item.get("server_filename") or "file"),
                "size": int(item.get("size") or 0),
                "dlink": item.get("dlink") or "",
                "is_dir": str(item.get("isdir")) == "1",
            })
        real = [f for f in files if not f["is_dir"]]
        if not real:
            raise TeraboxError("this share contains no downloadable files "
                               "(folders-only shares are not supported yet)")
        return real


async def download(url: str, dest_dir: str, index: int = 0,
                   progress_cb=None) -> str:
    """Download one file (by list index) from a Terabox share."""
    files = await list_files(url)
    if index >= len(files):
        index = 0
    chosen = files[index]
    if not chosen["dlink"]:
        raise TeraboxError("Terabox did not return a direct link for "
                           f"{chosen['name']} (cookie may lack download rights)")
    return await download_file(
        chosen["dlink"], dest_dir, progress_cb=progress_cb,
        filename=chosen["name"], cookies=_cookies(),
        headers={"Referer": API_ROOT + "/"},
    )
