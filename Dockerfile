FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/root/.deno \
    PATH=/root/.deno/bin:$PATH

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        git \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh && \
    deno upgrade --version 2.9.7 && \
    deno --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY auth.py .
COPY downloader.py .
COPY spotify.py .
COPY start.sh .

RUN chmod +x /app/start.sh

RUN mkdir -p /app/downloads && \
    chmod 755 /app/downloads

CMD ["/app/start.sh"]
