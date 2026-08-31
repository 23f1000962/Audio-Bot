# Cloudflare Containers-compatible Linux/amd64 image
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# FFmpeg is required for MP3 extraction.
# Node.js is used by yt-dlp as a JavaScript runtime for current YouTube extraction.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN mkdir -p /app/downloads

CMD ["python", "bot.py"]
