#!/bin/sh
set -eu

echo "============================================"
echo " Audio Bot starting"
echo "============================================"

# Reduce allocator fragmentation on small containers.
export PYTHONMALLOC=malloc
export MALLOC_ARENA_MAX=2
export DENO_NO_UPDATE_CHECK=1

echo "[1/2] Checking FFmpeg..."
ffmpeg -version | head -n 1

echo "[2/2] Checking Deno..."
deno --version

echo "============================================"
echo "Starting Telegram bot"
echo "============================================"

cd /app
exec python bot.py
