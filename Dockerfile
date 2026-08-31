FROM python:3.13-slim

# FFmpeg: MP3 extraction
# Node.js: JavaScript runtime used by current yt-dlp YouTube challenge handling
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -U -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
