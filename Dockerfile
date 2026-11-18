FROM python:3.11-slim

# ffmpeg powers yt-dlp merging and the bot's video thumbnails/probing;
# mediainfo powers the /mediainfo partial-probe reports; p7zip-full
# powers .rar/.7z listing + extraction (core/unzip.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg mediainfo p7zip-full \
    && rm -rf /var/lib/apt/lists/*

# Playwright + Chromium for the headless browser scraper
# (core/extractors/browser_scan.py). Installed as root because
# --with-deps installs system libraries via apt; the browsers live in
# /ms-playwright (PLAYWRIGHT_BROWSERS_PATH) so the runtime user
# (UID 1000, below) can read them — Playwright's default cache under
# the build user's $HOME would be invisible to it.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright==1.65.2 \
    && apt-get update \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

# Hugging Face Spaces run the container as user ID 1000, so the app
# must live in that user's home with files owned by it: a root-owned
# /app would leave the bot unable to write its downloads, data, or job
# dirs. This layout runs fine on Koyeb too (same image, same user).
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
# --user: as UID 1000 the system site-packages is not writable; the
# console scripts (gallery-dl, yt-dlp) land in ~/.local/bin, which is
# on PATH above.
RUN pip install --user --no-cache-dir -r requirements.txt

COPY --chown=user . .

# 7860 is the port Hugging Face Spaces route to; Koyeb sets PORT=8080
# in its own config, so one image serves both hosts.
EXPOSE 7860
CMD ["python", "main.py"]
