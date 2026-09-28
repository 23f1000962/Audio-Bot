#!/bin/sh
set -eu

export PYTHONMALLOC=malloc
export MALLOC_ARENA_MAX=2
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=192}"

mkdir -p "${YOUTUBE_ENGINE_DATA:-/tmp/audio-bot-youtube}"

node /app/youtube_engine.mjs > /tmp/youtube-engine.log 2>&1 &
ENGINE_PID=$!

cleanup() {
  kill "$ENGINE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${YOUTUBE_ENGINE_PORT:-8765}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "http://127.0.0.1:${YOUTUBE_ENGINE_PORT:-8765}/health" >/dev/null 2>&1; then
  echo "YouTube.js engine failed to start"
  cat /tmp/youtube-engine.log || true
  exit 1
fi

echo "============================================"
echo " Audio Bot v4.0 — Android OAuth"
echo " YouTube engine: YouTube.js"
echo " Authentication: OAuth device flow"
echo "============================================"

exec python /app/bot.py
