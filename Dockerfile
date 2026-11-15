FROM python:3.11-slim

# ffmpeg powers yt-dlp merging and the bot's video thumbnails/probing;
# mediainfo powers the /mediainfo partial-probe reports.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg mediainfo \
    && rm -rf /var/lib/apt/lists/*

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
