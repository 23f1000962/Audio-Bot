#!/bin/sh

set -eu

echo "============================================"
echo " Audio Bot starting"
echo "============================================"

echo "[1/4] Checking FFmpeg..."

ffmpeg -version | head -n 1


echo "[2/4] Checking Deno..."

deno --version


echo "[3/4] Starting BGUTIL PO Token Provider..."

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


echo "Waiting for BGUTIL provider..."

MAX_ATTEMPTS=30
ATTEMPT=1

while [ "${ATTEMPT}" -le "${MAX_ATTEMPTS}" ]; do

    # Check that the provider process is still alive.
    if ! kill -0 "${BGUTIL_PID}" 2>/dev/null; then

        echo "ERROR: BGUTIL provider stopped unexpectedly."

        exit 1

    fi

    # Check the actual HTTP health endpoint.
    if curl \
        --silent \
        --show-error \
        --fail \
        --max-time 2 \
        http://127.0.0.1:4416/ping \
        > /tmp/bgutil-health.json 2>/dev/null
    then

        echo "BGUTIL provider is ready."

        cat /tmp/bgutil-health.json

        break

    fi

    echo "BGUTIL not ready yet (${ATTEMPT}/${MAX_ATTEMPTS})..."

    ATTEMPT=$((ATTEMPT + 1))

    sleep 1

done


if [ "${ATTEMPT}" -gt "${MAX_ATTEMPTS}" ]; then

    echo "ERROR: BGUTIL provider did not become ready."

    echo "Last provider health response:"

    cat /tmp/bgutil-health.json 2>/dev/null || true

    exit 1

fi


echo "BGUTIL PO Token Provider is running on:"
echo "http://127.0.0.1:4416"


echo "============================================"
echo "[4/4] Starting Telegram bot"
echo "============================================"

cd /app

exec python bot.py