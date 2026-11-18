"""Custom-site extractor registry.

Sites yt-dlp does not understand get a native extractor here
instead of a browser (which can never run on Koyeb free).
Each entry is a module with three functions:

  is_site_url(url) -> bool
  resolve(url) -> {title, media_url, approx_size, is_playlist}
  download(url, job_dir, progress_cb=None, cancel_check=None)
      -> list[str] (file paths). `cancel_check`, when given, is a
      no-arg callable returning True when the owner stopped the job
      — modules using yt-dlp pass it through so the download aborts
      mid-file (v1.45.0); direct downloads abort via the
      progress_cb raising JobCancelled instead.

The uploader tries these BEFORE yt-dlp and AFTER Terabox:
a site yt-dlp understands should never be registered here —
registering it would only shadow a maintained extractor with
a fragile custom one. When a registered site changes its
player and its extractor breaks, only that site's jobs fail;
the resolver chain falls through to the next engine with an
honest error, and the other sites keep working.

To add a site: write its module, add one line to SITES below,
and add its flow to the uploader's dispatch (the custom flow
there is site-agnostic — it only needs the module).

A module whose URL matched but whose media turns out to belong to
another engine raises FallThrough (player_scan) from resolve() or
download() — a Reddit link to a YouTube video, a deleted post, an
API hiccup. Both dispatch sites catch it and continue the chain
instead of failing the job.
"""

from core.extractors import (buzzheavier, fichier, filehosts, firestream,
                             fuckingfast, github, gofile, hubcloud,
                             krakenfiles, lulacloud, luluvido, mediafire,
                             missav, pixeldrain, reddit_oauth, solidfiles,
                             sourceforge, streamtape, uploadhaven, uploadee,
                             vidara)

SITES = [
    # Player pages that need their own JS replayed (no yt-dlp extractor).
    streamtape,
    vidara,
    # File hosts / shorteners whose page hides the file behind a gate.
    filehosts,
    # Direct file hosts with their own page/API flow (no yt-dlp
    # extractor): mediafire pages, gofile shares, hubcloud /
    # hubdrive / gdflix movie-link hosts.
    mediafire,
    gofile,
    hubcloud,
    # LuluStream's packed-jwplayer pages (and its clone panels:
    # lulustream.com, lplaygo.com).
    luluvido,
    # FireStream / frstream.life: the page ships a one-time token that
    # is traded for the signed stream URL over their own JSON API.
    firestream,
    # MissAV: the stream URL sits in a packed player script, behind a
    # Cloudflare check and a Referer-locked surrit.com CDN.
    missav,
    # Reddit via its official OAuth API — the deliberate exception to
    # "never register a site yt-dlp understands": yt-dlp's Reddit path
    # is the scrape Reddit blocks on datacenter IPs. Only matches
    # when REDDIT_CLIENT_ID/SECRET are set; otherwise gallery-dl
    # handles Reddit exactly as before.
    reddit_oauth,
    # WZML-X round-3 hosts (review 2026-11-18): public-API hosts first
    # (pixeldrain file API, GitHub releases API, SourceForge mirror
    # picker), then the page-scrape hosts whose flow the generic gate
    # walker cannot drive (buzzheavier's htmx download button,
    # fuckingfast's window.open link, lulacloud's POST-redirect,
    # krakenfiles' token form, solidfiles' viewer JSON, upload.ee's
    # direct anchor, 1fichier's POST button, uploadhaven's timed
    # form). Each module's docstring says what was verified live.
    pixeldrain,
    github,
    sourceforge,
    buzzheavier,
    fuckingfast,
    lulacloud,
    krakenfiles,
    solidfiles,
    uploadee,
    fichier,
    uploadhaven,
]


def match(url: str):
    """Return the extractor module for `url`, or None."""
    for site in SITES:
        try:
            if site.is_site_url(url):
                return site
        except Exception:
            continue
    return None
