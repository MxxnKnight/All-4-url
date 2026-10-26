"""Custom-site extractor registry.

Sites yt-dlp does not understand get a native extractor here
instead of a browser (which can never run on Koyeb free).
Each entry is a module with three functions:

  is_site_url(url) -> bool
  resolve(url) -> {title, media_url, approx_size, is_playlist}
  download(url, job_dir, progress_cb=None) -> list[str] (file paths)

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
"""

from core.extractors import luluvido

SITES = [
    luluvido,
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
