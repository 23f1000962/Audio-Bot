#!/bin/sh
set -eu

echo "============================================"
echo " Audio Bot starting"
echo "============================================"

export PYTHONMALLOC=malloc
export MALLOC_ARENA_MAX=2
export DENO_NO_UPDATE_CHECK=1

# Keep the runtime cookie path writable for the personal /login flow.
export YOUTUBE_RUNTIME_COOKIE_FILE="${YOUTUBE_RUNTIME_COOKIE_FILE:-/tmp/youtube-cookies.txt}"

# New files created by the bot should be private by default.
umask 077

echo "[1/2] Checking FFmpeg..."
ffmpeg -version | head -n 1

echo "[2/2] Checking Deno..."
deno --version

echo "============================================"
echo "Starting Telegram bot"
echo "============================================"

cd /app
exec python bot.py
