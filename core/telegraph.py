"""Minimal Telegraph publisher (https://telegra.ph) over aiohttp.

Used to hand the owner a full MediaInfo report as a readable page —
far too long for a Telegram message. Accounts are free and anonymous:
we create one lazily per process and cache its token in a global (the
pages it creates stay public even after this process restarts).
"""

import aiohttp

API_BASE = "https://api.telegra.ph"
SHORT_NAME = "All4URL"
AUTHOR_NAME = "All-4-URL Bot"

_access_token: str | None = None

# Telegraph content nodes are JSON; a <pre> node per chunk of text keeps
# the report monospaced and readable. ~3500 chars per node stays well
# inside Telegraph's node/page limits for typical reports.
CHUNK_CHARS = 3500


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split text into <= limit-char pieces, breaking on line boundaries."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:  # pathological single line: hard-split it
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]


def page_content(text: str) -> list[dict]:
    """Telegraph node list for a plain-text report."""
    return [{"tag": "pre", "children": [chunk]} for chunk in chunk_text(text)]


async def _api(session: aiohttp.ClientSession, method: str,
               payload: dict) -> dict:
    async with session.post(f"{API_BASE}/{method}", json=payload) as resp:
        data = await resp.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph {method} failed: "
                           f"{data.get('error') or data}")
    return data["result"]


async def _get_token(session: aiohttp.ClientSession) -> str:
    global _access_token
    if _access_token:
        return _access_token
    result = await _api(session, "createAccount", {
        "short_name": SHORT_NAME,
        "author_name": AUTHOR_NAME,
    })
    _access_token = result["access_token"]
    return _access_token


async def create_page(title: str, text: str) -> str:
    """Publish `text` as a Telegraph page; returns its public URL."""
    async with aiohttp.ClientSession() as session:
        token = await _get_token(session)
        result = await _api(session, "createPage", {
            "access_token": token,
            "title": (title or "MediaInfo")[:256],
            "author_name": AUTHOR_NAME,
            "content": page_content(text),
            "return_content": False,
        })
    return f"https://telegra.ph/{result['path']}"
