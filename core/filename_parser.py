"""Parse a messy release filename into a clean title and its parts.

Downloaded files arrive with names like
"Show.S01E01.720p.WEB-DL.x264-GROUP.mkv" or
"www.example.com - Movie Name 2024 1080p BluRay.mkv". This module pulls the
useful bits out — title, year, season/episode, quality — so captions and the
unzip prompt can show "Show S01E01" instead of the raw mess.

Pure Python, standard library only; no network, no config. The behaviour is
inspired by how mirror bots clean filenames (WZML-X's poster-title cleaner is
the reference idea), reimplemented here from scratch.
"""

import re
from urllib.parse import unquote, urlsplit

__all__ = ["parse_filename", "clean_display"]

# File extensions worth stripping from the end of a name. Anything else
# (like ".E01") is left alone — it may be part of the title or a marker.
_EXTENSIONS = {
    "mkv", "mp4", "m4v", "avi", "webm", "mov", "wmv", "flv", "mpg", "mpeg",
    "3gp", "ts", "m2ts", "vob", "mp3", "flac", "aac", "ogg", "opus", "wav",
    "m4a", "zip", "rar", "7z", "tar", "gz", "srt", "ass", "vtt", "sub",
    "jpg", "jpeg", "png", "gif", "webp", "bmp",
}

# Tokens dropped from the title: quality/source/codec/audio/subtitle tags
# and container names. All lowercase; matched case-insensitively.
# Note "web-dl" and "blu-ray" arrive here already split ("web dl",
# "blu ray"), so their halves are listed separately.
_TAGS = {
    "web", "dl", "webdl", "webrip", "bluray", "brrip", "br", "rip", "blu",
    "ray", "hdtv", "hdrip", "dvdrip", "dvd", "cam", "camrip", "hdcam",
    "hdts", "remux", "repack", "proper", "unrated", "extended", "uncut",
    "remastered", "x264", "x265", "h264", "h265", "hevc", "avc", "xvid",
    "divx", "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "atmos", "dd",
    "ddp", "dd5", "hdr", "hdr10", "10bit", "8bit", "12bit", "dolby",
    "vision", "dovi", "esub", "esubs", "msub", "msubs", "subbed", "dubbed",
    "subs", "sub", "multi", "dual", "audio", "mkv", "mp4", "m4v", "avi",
    "webm", "mov", "wmv", "mpg", "mpeg", "zip", "rar", "7z", "tar", "gz",
    "srt", "ass", "ts", "tc",
}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_SITE_PREFIX_RE = re.compile(
    r"^\s*(?:www\.)?[\w-]+\.(?:com|net|org|xyz|me|in|to|co|cc|info|tv|link"
    r"|app|online|site|club|work|icu|top|vip|pro|party|fun|cam|lol|sbs|ws)"
    r"(?:\s*[-–—]+\s*|\s+)",
    re.IGNORECASE,
)
_PART_SUFFIX_RE = re.compile(r"\.\d{3}$")
_CHANNEL_RE = re.compile(r"(?<![0-9])[57]\.1\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_QUALITY_RE = re.compile(
    r"\b(?:4320|2160|1080|720|540|480)p\b|\b4k\b", re.IGNORECASE)
_RESOLUTION_TOKEN_RE = re.compile(r"^(?:\d{3,4}p|4k)$", re.IGNORECASE)
_SE_RE = re.compile(
    r"(?<![A-Za-z0-9])S(\d{1,2})\s*E(\d{1,3})(?![0-9])", re.IGNORECASE)
_X_RE = re.compile(r"(?<![0-9])(\d{1,2})x(\d{1,3})(?![0-9])", re.IGNORECASE)
_SEASON_WORD_RE = re.compile(r"\bSeason\s+(\d{1,2})\b", re.IGNORECASE)
_EPISODE_WORD_RE = re.compile(r"\b(?:Episode|Ep)\s*(\d{1,3})\b", re.IGNORECASE)
_S_ONLY_RE = re.compile(
    r"(?<![A-Za-z0-9])S(\d{1,2})(?![0-9A-Za-z])", re.IGNORECASE)
_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


def _strip_noise(name: str) -> str:
    """URLs, [bracketed] tags, site prefixes, split suffix, extension."""
    text = name.strip()
    # A name that IS a link: keep its last path segment as the filename.
    urls = _URL_RE.findall(text)
    text = _URL_RE.sub(" ", text)
    if not text.strip() and urls:
        segment = urlsplit(urls[0]).path.rsplit("/", 1)[-1]
        text = unquote(segment)
    text = _BRACKET_RE.sub(" ", text)
    text = _SITE_PREFIX_RE.sub("", text)
    text = _PART_SUFFIX_RE.sub("", text)
    base, dot, ext = text.rpartition(".")
    if dot and ext.lower() in _EXTENSIONS:
        text = base
    return text


def _despace(text: str) -> str:
    """Separators become spaces; surround-sound markers go away."""
    text = _CHANNEL_RE.sub(" ", text)
    text = re.sub(r"[._\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_season_episode(text: str):
    """(season, episode, marker_start) — ints or None, start or None.

    Shapes, in priority order: S01E02, 1x02, "Season 1" / "Episode 5"
    wording, then a bare S01 (season only).
    """
    match = _SE_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), match.start()
    match = _X_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), match.start()
    season = episode = None
    start = None
    match = _SEASON_WORD_RE.search(text)
    if match:
        season = int(match.group(1))
        start = match.start()
    match = _EPISODE_WORD_RE.search(text)
    if match:
        episode = int(match.group(1))
        start = match.start() if start is None else min(start, match.start())
    if season is None and episode is None:
        match = _S_ONLY_RE.search(text)
        if match:
            return int(match.group(1)), None, match.start()
    return season, episode, start


def _clean_tokens(title_text: str, year: str | None) -> str:
    """Drop tag tokens, resolution tokens and the year itself."""
    words = []
    for token in title_text.split():
        word = token.strip("()[]{}'\".,;:!?")
        if not word:
            continue
        low = word.lower()
        if low in _TAGS:
            continue
        if _RESOLUTION_TOKEN_RE.match(low):
            continue
        if year and low == year:
            continue
        words.append(word)
    return " ".join(words)


def _display(title: str, year: str | None, season: int | None,
             episode: int | None, fallback: str) -> str:
    """The human-readable label, per the parse that succeeded."""
    if season is not None and episode is not None:
        marker = f"S{season:02d}E{episode:02d}"
    elif season is not None:
        marker = f"Season {season}"
    elif episode is not None:
        marker = f"E{episode:02d}"
    else:
        marker = ""
    if title and marker:
        return f"{title} {marker}"
    if title and year:
        return f"{title} ({year})"
    if title:
        return title
    if marker:
        return marker
    return fallback


def parse_filename(name: str) -> dict:
    """Parse a filename into its parts.

    Returns {"title": str, "year": str | None, "season": int | None,
    "episode": int | None, "quality": str | None, "clean": str}.
    "title" is "" when nothing meaningful remains (a bare S01E01, a
    hash-like name); "clean" is the display string, falling back to
    the de-separated basename when nothing parses at all.
    """
    empty = {"title": "", "year": None, "season": None, "episode": None,
             "quality": None, "clean": ""}
    raw = (name or "").strip()
    if not raw:
        return dict(empty)
    # Quality is read BEFORE bracketed tags are stripped — release
    # names often carry it as [1080p], and stripping first would
    # lose it.
    quality = None
    quality_match = _QUALITY_RE.search(_despace(raw))
    if quality_match:
        found = quality_match.group(0).lower()
        quality = "2160p" if found == "4k" else found
    text = _despace(_strip_noise(raw))
    if not text:
        return dict(empty)

    season, episode, marker_start = _find_season_episode(text)

    # The year is the LAST 4-digit 19xx/20xx token — so "2001 A Space
    # Odyssey 1968" keeps 2001 in the title and reports 1968.
    years = list(_YEAR_RE.finditer(text))
    year = years[-1].group(0) if years else None
    year_start = years[-1].start() if years else None

    quality_start = None
    if quality:
        # Boundary for the title cut: where the quality tag sits in
        # the cleaned text (None when it only existed in brackets).
        m2 = _QUALITY_RE.search(text)
        if m2:
            quality_start = m2.start()

    # The title is the words before the first boundary — S/E marker,
    # year, or quality tag, whichever comes first.
    bounds = [b for b in (marker_start, year_start, quality_start)
              if b is not None]
    title_text = text[:min(bounds)] if bounds else text
    title = _clean_tokens(title_text, year)

    # A long hex token is a hash, not a title (16+ hex chars).
    if title and _HEX_RE.match(title):
        title = ""

    return {"title": title, "year": year, "season": season,
            "episode": episode, "quality": quality,
            "clean": _display(title, year, season, episode, text)}


def clean_display(name: str) -> str:
    """Just the display string from parse_filename."""
    return parse_filename(name)["clean"]
