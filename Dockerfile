FROM python:3.13-slim

# ============================================================
# ENVIRONMENT
# ============================================================

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PIP_NO_CACHE_DIR=1

# ============================================================
# WORKING DIRECTORY
# ============================================================

WORKDIR /app

# ============================================================
# SYSTEM DEPENDENCIES
# ============================================================

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# PYTHON DEPENDENCIES
# ============================================================

COPY requirements.txt .

RUN pip install --no-cache-dir \
    -r requirements.txt

# ============================================================
# APPLICATION
# ============================================================

COPY bot.py .
COPY downloader.py .

# ============================================================
# DOWNLOAD DIRECTORY
# ============================================================

RUN mkdir -p /app/downloads && \
    chmod 755 /app/downloads

# ============================================================
# HEALTH CHECK
# ============================================================

HEALTHCHECK --interval=30s \
    --timeout=10s \
    --start-period=30s \
    --retries=3 \
    CMD curl -f http://localhost:${PORT}/healthz || exit 1

# ============================================================
# START BOT
# ============================================================

CMD ["python", "bot.py"]