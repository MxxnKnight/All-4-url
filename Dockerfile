FROM python:3.11-slim

# ffmpeg powers yt-dlp merging and the bot's video thumbnails/probing;
# mediainfo powers the /mediainfo partial-probe reports.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg mediainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["python", "main.py"]
