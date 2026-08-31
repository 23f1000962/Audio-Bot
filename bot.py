import os
import re
import json
import time
import shutil
import asyncio
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


MODEL_VERSION = "0.5.3"

BOT_TOKEN = os.getenv("BOT_TOKEN")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
PORT = int(os.getenv("PORT", "8080"))

# Correct RapidAPI host from the RapidAPI code snippet.
RAPIDAPI_HOST = os.getenv(
    "RAPIDAPI_HOST",
    "youtube-mp4-mp3-downloader.p.rapidapi.com",
)

RAPIDAPI_BASE_URL = os.getenv(
    "RAPIDAPI_BASE_URL",
    f"https://{RAPIDAPI_HOST}",
)

AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "mp3")
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "128")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "600"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Keep downloads below the bot's configured Telegram limit.
MAX_TELEGRAM_AUDIO_BYTES = 49 * 1024 * 1024


YOUTUBE_REGEX = re.compile(
    r"^(?:https?://)?"
    r"(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com|youtu\.be)/.+",
    re.IGNORECASE,
)


def format_bytes(size):
    if size is None:
        return "Unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def normalize_youtube_url(url: str) -> str:
    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    path_parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    # YouTube Shorts.
    if (
        "youtube.com" in hostname
        and len(path_parts) >= 2
        and path_parts[0].lower() == "shorts"
    ):
        return (
            "https://www.youtube.com/watch?v="
            f"{path_parts[1]}"
        )

    # Short youtu.be URLs.
    if "youtu.be" in hostname and path_parts:
        return (
            "https://www.youtube.com/watch?v="
            f"{path_parts[0]}"
        )

    # Standard YouTube watch URLs.
    if (
        "youtube.com" in hostname
        and parsed.path == "/watch"
    ):
        video_ids = parse_qs(
            parsed.query
        ).get("v")

        if video_ids:
            return (
                "https://www.youtube.com/watch?v="
                f"{video_ids[0]}"
            )

    return url


def youtube_video_id(url: str) -> str:
    normalized = normalize_youtube_url(url)

    parsed = urlparse(normalized)

    video_ids = parse_qs(
        parsed.query
    ).get("v")

    if not video_ids:
        raise ValueError(
            "Could not determine the YouTube video ID."
        )

    return video_ids[0]


async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


def rapidapi_headers():
    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def request_download(video_id: str) -> dict:
    """
    Start the RapidAPI download request.

    Correct endpoint:
    /api/v1/download
    """

    response = requests.get(
        f"{RAPIDAPI_BASE_URL}/api/v1/download",
        params={
            "format": AUDIO_FORMAT,
            "id": video_id,
            "audioQuality": AUDIO_QUALITY,
            "addInfo": "false",
            "allowExtendedDuration": "false",
        },
        headers=rapidapi_headers(),
        timeout=60,
    )

    response.raise_for_status()

    try:
        data = response.json()

    except ValueError as exc:
        raise RuntimeError(
            "RapidAPI returned invalid JSON: "
            f"{response.text[:300]}"
        ) from exc

    if data.get("success") is False:
        raise RuntimeError(
            data.get("message")
            or data.get("error")
            or "RapidAPI rejected the download request."
        )

    return data


def request_progress(progress_id: str) -> dict:
    """
    Check the processing status.

    Correct endpoint:
    /api/v1/progress?id=...
    """

    response = requests.get(
        f"{RAPIDAPI_BASE_URL}/api/v1/progress",
        params={
            "id": progress_id,
        },
        headers=rapidapi_headers(),
        timeout=60,
    )

    response.raise_for_status()

    try:
        return response.json()

    except ValueError as exc:
        raise RuntimeError(
            "RapidAPI progress endpoint returned invalid JSON: "
            f"{response.text[:300]}"
        ) from exc


def first_value(data, keys):
    """
    Recursively search dictionaries/lists for the first
    non-empty value matching one of the provided keys.
    """

    if isinstance(data, dict):

        for key in keys:
            value = data.get(key)

            if value not in (None, ""):
                return value

        for value in data.values():

            found = first_value(
                value,
                keys,
            )

            if found not in (None, ""):
                return found

    elif isinstance(data, list):

        for value in data:

            found = first_value(
                value,
                keys,
            )

            if found not in (None, ""):
                return found

    return None


def find_download_url(data):
    """
    Search for possible download URL fields returned by
    different API response formats.
    """

    keys = (
        "downloadUrl",
        "download_url",
        "url",
        "fileUrl",
        "file_url",
        "audioUrl",
        "audio_url",
        "link",
    )

    value = first_value(
        data,
        keys,
    )

    if (
        isinstance(value, str)
        and value.startswith(
            ("http://", "https://")
        )
    ):
        return value

    return None


def progress_is_failed(data):
    status = str(
        first_value(
            data,
            (
                "status",
                "state",
                "progressStatus",
            ),
        )
        or ""
    ).lower()

    if status in {
        "failed",
        "error",
        "cancelled",
        "canceled",
    }:
        return True

    return data.get("success") is False


def progress_percent(data):
    value = first_value(
        data,
        (
            "progress",
            "percent",
            "percentage",
            "completion",
        ),
    )

    if value is None:
        return None

    try:
        value = float(value)

        # Some APIs return 0-1 instead of 0-100.
        if 0 <= value <= 1:
            value *= 100

        return max(
            0,
            min(100, value),
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def download_audio_file(
    download_url: str,
    video_id: str,
) -> Path:

    safe_id = re.sub(
        r"[^A-Za-z0-9_-]",
        "_",
        video_id,
    )

    output_path = (
        DOWNLOAD_DIR
        / f"{safe_id}.mp3"
    )

    with requests.get(
        download_url,
        stream=True,
        timeout=(30, 300),
        allow_redirects=True,
    ) as response:

        response.raise_for_status()

        content_length = response.headers.get(
            "Content-Length"
        )

        if content_length:

            try:
                size = int(content_length)

                if size > MAX_TELEGRAM_AUDIO_BYTES:
                    raise ValueError(
                        f"Audio is {format_bytes(size)}, "
                        "which is larger than this bot's "
                        "configured Telegram upload limit."
                    )

            except ValueError as exc:

                if (
                    "configured Telegram upload limit"
                    in str(exc)
                ):
                    raise

        total = 0

        with open(
            output_path,
            "wb",
        ) as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 256
            ):

                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_TELEGRAM_AUDIO_BYTES:

                    output.close()

                    try:
                        output_path.unlink()

                    except FileNotFoundError:
                        pass

                    raise ValueError(
                        "The downloaded audio is too large "
                        "for this bot's configured Telegram "
                        "upload limit."
                    )

                output.write(chunk)

    return output_path


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        f"🎵 Audio Bot v{MODEL_VERSION} is online.\n\n"
        "Send me a YouTube video or YouTube Shorts "
        "link and I'll download the available audio "
        "and send it as MP3.\n\n"
        "Please only download content you own or are "
        "authorized to download."
    )


async def version_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        f"🤖 Audio Bot version: v{MODEL_VERSION}\n"
        "✅ RapidAPI download service enabled."
    )


async def greeting(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        f"👋 Hey! Audio Bot v{MODEL_VERSION} "
        "is online and running."
    )


async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    original_url = (
        update.message.text
        or ""
    ).strip()

    if not YOUTUBE_REGEX.match(
        original_url
    ):

        await update.message.reply_text(
            "❌ Please send a valid YouTube or "
            "YouTube Shorts link.\n\n"
            f"🤖 Audio Bot v{MODEL_VERSION}"
        )

        return

    if not RAPIDAPI_KEY:

        await update.message.reply_text(
            "❌ The bot server is missing "
            "RAPIDAPI_KEY configuration."
        )

        return

    status_message = (
        await update.message.reply_text(
            f"🔍 Checking the link...\n"
            f"🤖 v{MODEL_VERSION}"
        )
    )

    audio_path = None

    try:

        url = normalize_youtube_url(
            original_url
        )

        video_id = youtube_video_id(
            url
        )

        await safe_edit(
            status_message,
            f"⬇️ Sending download request...\n"
            f"🤖 v{MODEL_VERSION}",
        )

        # Start RapidAPI download request.
        request_data = (
            await asyncio.to_thread(
                request_download,
                video_id,
            )
        )

        print(
            "Download response:",
            json.dumps(
                request_data,
                indent=2,
            )[:2000],
        )

        title = (
            first_value(
                request_data,
                (
                    "title",
                    "name",
                ),
            )
            or "audio"
        )

        # Get the processing ID.
        progress_id = first_value(
            request_data,
            (
                "progressId",
                "progress_id",
                "progressID",
                "jobId",
                "job_id",
                "taskId",
                "task_id",
            ),
        )

        download_url = find_download_url(
            request_data
        )

        # Poll the progress endpoint if a direct URL
        # was not returned immediately.
        if not download_url:

            if not progress_id:
                raise RuntimeError(
                    "RapidAPI did not return a progress ID "
                    "or download URL. Response: "
                    f"{json.dumps(request_data)[:600]}"
                )

            last_percent = None
            started = time.time()

            while True:

                if (
                    time.time() - started
                    > POLL_TIMEOUT
                ):
                    raise TimeoutError(
                        "Timed out waiting for RapidAPI "
                        "to prepare the audio."
                    )

                progress_data = (
                    await asyncio.to_thread(
                        request_progress,
                        progress_id,
                    )
                )

                print(
                    "Progress response:",
                    json.dumps(
                        progress_data,
                        indent=2,
                    )[:2000],
                )

                if progress_is_failed(
                    progress_data
                ):
                    raise RuntimeError(
                        first_value(
                            progress_data,
                            (
                                "message",
                                "error",
                                "reason",
                            ),
                        )
                        or "RapidAPI reported that "
                        "processing failed."
                    )

                progress_title = first_value(
                    progress_data,
                    (
                        "title",
                        "name",
                    ),
                )

                if progress_title:
                    title = progress_title

                download_url = find_download_url(
                    progress_data
                )

                if download_url:
                    break

                percent = progress_percent(
                    progress_data
                )

                if percent is not None:

                    rounded = int(percent)

                    if rounded != last_percent:

                        last_percent = rounded

                        await safe_edit(
                            status_message,
                            f"⏳ Preparing audio: "
                            f"{rounded}%\n"
                            f"🤖 v{MODEL_VERSION}",
                        )

                else:

                    await safe_edit(
                        status_message,
                        "⏳ Preparing audio...\n"
                        f"🤖 v{MODEL_VERSION}",
                    )

                await asyncio.sleep(
                    POLL_INTERVAL
                )

        if not download_url:
            raise RuntimeError(
                "RapidAPI finished without providing "
                "a download URL."
            )

        await safe_edit(
            status_message,
            "⬇️ Downloading the audio file...\n"
            f"🤖 v{MODEL_VERSION}",
        )

        audio_path = (
            await asyncio.to_thread(
                download_audio_file,
                download_url,
                video_id,
            )
        )

        if not audio_path.exists():
            raise FileNotFoundError(
                "The audio file was not created."
            )

        size = audio_path.stat().st_size

        await safe_edit(
            status_message,
            f"📤 Uploading "
            f"{format_bytes(size)} to Telegram...\n"
            f"🤖 v{MODEL_VERSION}",
        )

        safe_title = (
            re.sub(
                r"[\r\n]+",
                " ",
                str(title),
            )
            .strip()[:64]
            or "audio"
        )

        filename_title = (
            re.sub(
                r'[\\/:*?"<>|]+',
                "_",
                safe_title[:50],
            )
            .strip()
        )

        with open(
            audio_path,
            "rb",
        ) as audio:

            await update.message.reply_audio(
                audio=audio,
                title=safe_title,
                filename=(
                    f"{filename_title or 'audio'}.mp3"
                ),
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
                pool_timeout=60,
            )

        await safe_edit(
            status_message,
            f"✅ Done!\n"
            f"🤖 Audio Bot v{MODEL_VERSION}",
        )

        await asyncio.sleep(2)

        try:
            await status_message.delete()

        except Exception:
            pass

    except Exception as error:

        print(
            f"Processing error: "
            f"{type(error).__name__}: {error}"
        )

        if (
            isinstance(error, ValueError)
            and "large" in str(error).lower()
        ):

            message = (
                "❌ This audio is too large for the "
                "bot's configured Telegram upload limit."
                "\n\n"
                f"🤖 Audio Bot v{MODEL_VERSION}"
            )

        else:

            message = (
                "❌ Sorry, I couldn't process this link. "
                "It may be unavailable, unsupported by "
                "the API, still processing, or too large "
                "for Telegram.\n\n"
                f"🤖 Audio Bot v{MODEL_VERSION}"
            )

        await safe_edit(
            status_message,
            message,
        )

    finally:

        if (
            audio_path
            and audio_path.exists()
        ):

            try:
                audio_path.unlink()

            except Exception as cleanup_error:

                print(
                    f"Cleanup error: "
                    f"{cleanup_error}"
                )


class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
            "/healthz",
        ):

            body = json.dumps(
                {
                    "status": "ok",
                    "version": MODEL_VERSION,
                    "service": (
                        "telegram-youtube-audio-bot"
                    ),
                    "provider": "rapidapi",
                }
            ).encode()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json",
            )

            self.send_header(
                "Content-Length",
                str(len(body)),
            )

            self.end_headers()

            self.wfile.write(body)

        else:

            self.send_response(404)
            self.end_headers()

    def log_message(
        self,
        format,
        *args,
    ):
        return


def start_health_server():

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT,
        ),
        HealthHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    print(
        f"Health server listening on "
        f"0.0.0.0:{PORT}"
    )


def cleanup_download_directory():

    if not DOWNLOAD_DIR.exists():
        return

    for item in DOWNLOAD_DIR.iterdir():

        try:

            if (
                item.is_file()
                or item.is_symlink()
            ):
                item.unlink()

            elif item.is_dir():
                shutil.rmtree(item)

        except Exception as error:

            print(
                f"Startup cleanup error: "
                f"{error}"
            )


def main():

    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN environment variable is not set."
        )

    if not RAPIDAPI_KEY:
        raise ValueError(
            "RAPIDAPI_KEY environment variable is not set."
        )

    cleanup_download_directory()

    start_health_server()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "version",
            version_command,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex(
                re.compile(
                    r"^\s*(?:hey|hello|hii|hi)"
                    r"\s*[!.]?\s*$",
                    re.I,
                )
            ),
            greeting,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_link,
        )
    )

    print(
        f"🤖 Audio Bot v{MODEL_VERSION} "
        "is running with RapidAPI..."
    )

    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()