"""Real movie/show posters from TMDB, for video thumbnails.

When the bot sends a video it normally grabs a frame as the thumbnail.
With a free TMDB API key or read-access token configured, it can use the
actual poster instead: search by the parsed title, take the first result
with a poster, download the w342 image.

Optional by design: with no token configured, get_poster returns None
immediately (no network) and the caller falls back to a frame thumbnail.
Nothing here ever raises — any failure is a silent None.
"""

import aiohttp

import config

_SEARCH_URL = "https://api.themoviedb.org/3/search/multi"
_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

# (title, year) -> poster bytes or None. A miss is cached too, so a
# title TMDB does not know is not re-queried for the bot's lifetime.
_CACHE: dict[tuple[str, str | None], bytes | None] = {}


async def get_poster(title: str, year: str | None = None) -> bytes | None:
    """Poster image bytes (w342) for the title, or None.

    The cache is keyed on the lowercased title plus year, so a repeat
    lookup for the same title never hits the network twice.
    """
    title = (title or "").strip()
    if not title:
        return None
    key = (title.lower(), year)
    if key in _CACHE:
        return _CACHE[key]
    result = await _fetch_poster(title, year)
    _CACHE[key] = result
    return result


async def _fetch_poster(title: str, year: str | None) -> bytes | None:
    """One search + one download; None on any failure, never raises."""
    token = (getattr(config, "TMDB_ACCESS_TOKEN", "") or "").strip()
    api_key = (getattr(config, "TMDB_API_KEY", "") or "").strip()
    if not token and not api_key:
        return None
    params = {"query": title, "include_adult": "false",
              "language": "en-US", "page": "1"}
    headers = {"accept": "application/json"}
    if token:
        # v4 read-access token goes in the Authorization header.
        headers["Authorization"] = f"Bearer {token}"
    else:
        # v3 API key goes in the query string.
        params["api_key"] = api_key
    if year:
        params["year"] = year
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_SEARCH_URL, params=params,
                                   headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            poster_path = None
            for result in data.get("results") or []:
                if result.get("poster_path"):
                    poster_path = result["poster_path"]
                    break
            if not poster_path:
                return None
            async with session.get(_IMAGE_BASE + poster_path) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception:
        return None
