FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PIP_NO_CACHE_DIR=1 \
    YOUTUBE_ENGINE_PORT=8765 \
    YOUTUBE_ENGINE_DATA=/tmp/audio-bot-youtube

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY package.json .
RUN npm install --omit=dev --no-audit --no-fund

COPY bot.py auth.py downloader.py spotify.py youtube_engine.mjs start.sh ./

RUN chmod +x /app/start.sh && mkdir -p /app/downloads

CMD ["/app/start.sh"]
