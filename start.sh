#!/bin/sh

set -eu

echo "============================================"
echo " Audio Bot starting"
echo "============================================"

echo "[1/3] Checking FFmpeg..."
ffmpeg -version | head -n 1

echo "[2/3] Checking Deno..."
deno --version

echo "[3/3] Starting BGUTIL PO Token Provider..."

cd /opt/bgutil/server/node_modules

deno run \
    --allow-env \
    --allow-net \
    --allow-ffi=. \
    --allow-read=. \
    ../src/main.ts \
    --host 127.0.0.1 \
    --port 4416 &

BGUTIL_PID=$!

echo "BGUTIL provider PID: ${BGUTIL_PID}"

# Give the provider a moment to initialize.
sleep 3

if ! kill -0 "${BGUTIL_PID}" 2>/dev/null; then
    echo "ERROR: BGUTIL provider failed to start."
    exit 1
fi

echo "BGUTIL PO Token Provider is running on 127.0.0.1:4416"

echo "============================================"
echo " Starting Telegram bot"
echo "============================================"

cd /app

exec python bot.py