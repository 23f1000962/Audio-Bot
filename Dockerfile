FROM python:3.13-slim

# ============================================================
# ENVIRONMENT
# ============================================================

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/root/.deno \
    PATH=/root/.deno/bin:$PATH \
    BGUTIL_HOME=/opt/bgutil

WORKDIR /app

# ============================================================
# SYSTEM DEPENDENCIES
# ============================================================

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# INSTALL DENO
# ============================================================

RUN curl -fsSL https://deno.land/install.sh | sh

RUN deno --version

# ============================================================
# PYTHON DEPENDENCIES
# ============================================================

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# ============================================================
# INSTALL BGUTIL PO TOKEN PROVIDER SERVER
# ============================================================

RUN git clone \
        --single-branch \
        --branch 2.0.0 \
        --depth 1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        ${BGUTIL_HOME}

WORKDIR ${BGUTIL_HOME}/server

# Install the provider's JavaScript dependencies.
RUN deno install \
        --allow-scripts=npm:canvas \
        --frozen

# ============================================================
# APPLICATION
# ============================================================

WORKDIR /app

COPY bot.py .
COPY downloader.py .
COPY start.sh .

RUN chmod +x /app/start.sh

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
    --start-period=45s \
    --retries=3 \
    CMD curl -f http://localhost:${PORT}/healthz || exit 1

# ============================================================
# START
# ============================================================

CMD ["/app/start.sh"]