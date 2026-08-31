import os
import re
import asyncio
import shutil
import time
import threading
import subprocess

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import yt_dlp

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIGURATION
# ============================================================

MODEL_VERSION = "0.4.0"

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Conservative upload limit.
MAX_TELEGRAM_AUDIO_BYTES = 49 * 1024 * 1024


YOUTUBE_REGEX = re.compile(
    r"^(?:https?://)?"
    r"(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com|youtu\.be)/.+",
    re.IGNORECASE,
)


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def format_bytes(size):
    """Convert bytes into readable format."""

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
    """
    Convert YouTube Shorts and shortened URLs
    into a standard YouTube watch URL.
    """

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

    # --------------------------------------------------------
    # YouTube Shorts
    #
    # youtube.com/shorts/VIDEO_ID
    # --------------------------------------------------------

    if (
        "youtube.com" in hostname
        and len(path_parts) >= 2
        and path_parts[0].lower() == "shorts"
    ):

        video_id = path_parts[1]

        return (
            f"https://www.youtube.com/watch?v="
            f"{video_id}"
        )

    # --------------------------------------------------------
    # Short YouTube URLs
    #
    # youtu.be/VIDEO_ID
    # --------------------------------------------------------

    if "youtu.be" in hostname and path_parts:

        video_id = path_parts[0]

        return (
            f"https://www.youtube.com/watch?v="
            f"{video_id}"
        )

    # --------------------------------------------------------
    # Standard watch URLs
    # --------------------------------------------------------

    if (
        "youtube.com" in hostname
        and parsed.path == "/watch"
    ):

        query = parse_qs(parsed.query)

        video_ids = query.get("v")

        if video_ids:

            return (
                f"https://www.youtube.com/watch?v="
                f"{video_ids[0]}"
            )

    return url


async def safe_edit(message, text):
    """Edit a Telegram message safely."""

    try:

        await message.edit_text(text)

    except Exception:

        pass


# ============================================================
# LARGE FILE COMPRESSION
# ============================================================

def compress_audio_to_fit(
    input_path: Path
) -> Path:
    """
    Compress audio progressively until it fits
    within Telegram's configured upload limit.
    """

    bitrates = [
        "128k",
        "96k",
        "64k",
    ]

    for bitrate in bitrates:

        output_path = input_path.with_name(
            f"{input_path.stem}_{bitrate}.mp3"
        )

        print(
            f"Trying compression at {bitrate}..."
        )

        command = [

            "ffmpeg",

            "-y",

            "-i",
            str(input_path),

            "-vn",

            "-c:a",
            "libmp3lame",

            "-b:a",
            bitrate,

            str(output_path),

        ]

        result = subprocess.run(

            command,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

        )

        # ----------------------------------------------------
        # FFmpeg failed
        # ----------------------------------------------------

        if result.returncode != 0:

            print(
                f"FFmpeg failed at {bitrate}:"
            )

            print(
                result.stderr.decode(
                    errors="ignore"
                )
            )

            continue

        # ----------------------------------------------------
        # Check compressed file
        # ----------------------------------------------------

        if output_path.exists():

            size = output_path.stat().st_size

            print(
                f"Compressed file size at "
                f"{bitrate}: "
                f"{format_bytes(size)}"
            )

            if (
                size
                <= MAX_TELEGRAM_AUDIO_BYTES
            ):

                return output_path

            # Delete versions that still don't fit.
            try:

                output_path.unlink()

            except Exception:

                pass

    raise ValueError(
        "The audio is too large even after "
        "compression."
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(

        f"🎵 Audio Bot v{MODEL_VERSION} is online.\n\n"

        "Send me a YouTube video or YouTube "
        "Shorts link and I'll extract the audio "
        "as an MP3.\n\n"

        "Please only download content you own "
        "or are authorized to download."

    )


async def version_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(

        f"🤖 Audio Bot version: "
        f"v{MODEL_VERSION}\n"

        "✅ Service is running."

    )


async def greeting(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(

        f"👋 Hey! Audio Bot v{MODEL_VERSION} "
        "is online and running."

    )


# ============================================================
# MAIN DOWNLOAD HANDLER
# ============================================================

async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    original_url = update.message.text.strip()

    # --------------------------------------------------------
    # Validate URL
    # --------------------------------------------------------

    if not YOUTUBE_REGEX.match(original_url):

        await update.message.reply_text(

            "❌ Please send a valid YouTube "
            "or YouTube Shorts link.\n\n"

            f"🤖 Audio Bot v{MODEL_VERSION}"

        )

        return

    # --------------------------------------------------------
    # Normalize URL
    # --------------------------------------------------------

    url = normalize_youtube_url(
        original_url
    )

    print(
        f"Original URL: {original_url}"
    )

    print(
        f"Normalized URL: {url}"
    )

    # --------------------------------------------------------
    # Status message
    # --------------------------------------------------------

    status_message = (
        await update.message.reply_text(

            "🔍 Checking the link...\n"

            f"🤖 v{MODEL_VERSION}"

        )
    )

    loop = asyncio.get_running_loop()

    progress_data = {

        "last_update": 0,

    }

    # ========================================================
    # DOWNLOAD PROGRESS
    # ========================================================

    def progress_hook(data):

        if (
            data.get("status")
            != "downloading"
        ):

            return

        now = time.time()

        if (
            now
            - progress_data["last_update"]
            < 2
        ):

            return

        progress_data[
            "last_update"
        ] = now

        downloaded = data.get(
            "downloaded_bytes",
            0,
        )

        total = (

            data.get("total_bytes")

            or

            data.get(
                "total_bytes_estimate"
            )

            or 0

        )

        speed = data.get(
            "speed"
        )

        percent = data.get(
            "_percent_str",
            "",
        ).strip()

        text = (
            f"⬇️ Downloading: "
            f"{percent}"
        )

        if downloaded:

            text += (

                f"\nDownloaded: "

                f"{format_bytes(downloaded)}"

            )

        if total:

            text += (

                f" / "

                f"{format_bytes(total)}"

            )

        if speed:

            text += (

                f"\nSpeed: "

                f"{format_bytes(speed)}/s"

            )

        text += (

            f"\n🤖 v{MODEL_VERSION}"

        )

        try:

            asyncio.run_coroutine_threadsafe(

                safe_edit(
                    status_message,
                    text,
                ),

                loop,

            )

        except Exception:

            pass


    # ========================================================
    # DOWNLOAD FUNCTION
    # ========================================================

    def download_audio():

        output_template = str(

            DOWNLOAD_DIR

            / "%(id)s.%(ext)s"

        )

        ydl_opts = {

            # Best available audio.
            "format":
                "bestaudio/best",

            "noplaylist":
                True,

            "outtmpl":
                output_template,

            "quiet":
                True,

            "no_warnings":
                True,

            # Network reliability.
            "socket_timeout":
                60,

            "retries":
                3,

            "fragment_retries":
                3,

            # Progress updates.
            "progress_hooks":
                [
                    progress_hook
                ],

            # Convert to MP3.
            "postprocessors":
                [

                    {

                        "key":
                            "FFmpegExtractAudio",

                        "preferredcodec":
                            "mp3",

                        "preferredquality":
                            "192",

                    }

                ],

        }

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(

                url,

                download=True,

            )

            video_id = info["id"]

            title = info.get(

                "title",

                "audio",

            )

            audio_path = (

                DOWNLOAD_DIR

                / f"{video_id}.mp3"

            )

            return (

                audio_path,

                title,

            )


    audio_path = None

    # ========================================================
    # PROCESS DOWNLOAD
    # ========================================================

    try:

        await safe_edit(

            status_message,

            "⬇️ Starting download...\n"

            f"🤖 v{MODEL_VERSION}",

        )

        # Download in a separate thread.
        audio_path, title = (
            await asyncio.to_thread(

                download_audio

            )
        )

        # ----------------------------------------------------
        # Check file
        # ----------------------------------------------------

        if not audio_path.exists():

            raise FileNotFoundError(

                "The audio file was not created."

            )

        size = audio_path.stat().st_size

        print(

            f"Original audio size: "

            f"{format_bytes(size)}"

        )

        # ====================================================
        # LARGE FILE HANDLING
        # ====================================================

        if (

            size
            > MAX_TELEGRAM_AUDIO_BYTES

        ):

            await safe_edit(

                status_message,

                f"📦 Audio size: "

                f"{format_bytes(size)}\n\n"

                "🔄 Compressing audio to fit "
                "Telegram...\n"

                "Trying lower quality automatically.\n"

                f"🤖 v{MODEL_VERSION}",

            )

            # Compress without blocking Telegram.
            compressed_path = (
                await asyncio.to_thread(

                    compress_audio_to_fit,

                    audio_path,

                )
            )

            # Delete original file.
            if (

                compressed_path
                != audio_path

                and

                audio_path.exists()

            ):

                try:

                    audio_path.unlink()

                except Exception:

                    pass

            # Use compressed file.
            audio_path = compressed_path

            size = (
                audio_path
                .stat()
                .st_size
            )

            print(

                f"Final audio size: "

                f"{format_bytes(size)}"

            )

        # ====================================================
        # UPLOAD TO TELEGRAM
        # ====================================================

        await safe_edit(

            status_message,

            f"📤 Uploading "

            f"{format_bytes(size)} "

            "to Telegram...\n"

            f"🤖 v{MODEL_VERSION}",

        )

        safe_title = (

            re.sub(

                r"[\r\n]+",

                " ",

                title,

            )

            .strip()[:64]

            or "audio"

        )

        with open(

            audio_path,

            "rb",

        ) as audio:

            await update.message.reply_audio(

                audio=audio,

                title=safe_title,

                filename=(
                    f"{safe_title[:50]}.mp3"
                ),

                read_timeout=300,

                write_timeout=300,

                connect_timeout=60,

                pool_timeout=60,

            )

        # ====================================================
        # SUCCESS
        # ====================================================

        await safe_edit(

            status_message,

            "✅ Done!\n"

            f"🤖 Audio Bot v{MODEL_VERSION}",

        )

        await asyncio.sleep(2)

        try:

            await status_message.delete()

        except Exception:

            pass


    # ========================================================
    # ERROR HANDLING
    # ========================================================

    except Exception as error:

        print(

            "Processing error: "

            f"{type(error).__name__}: "

            f"{error}"

        )

        if (

            isinstance(
                error,
                ValueError,
            )

            and

            "too large"

            in str(error).lower()

        ):

            message = (

                "❌ This audio is still too "
                "large for Telegram even after "
                "automatic compression.\n\n"

                f"🤖 Audio Bot "

                f"v{MODEL_VERSION}"

            )

        else:

            message = (

                "❌ Sorry, I couldn't process "
                "this link.\n"

                "It may be unavailable, "
                "blocked by YouTube, "
                "or too large for Telegram.\n\n"

                f"🤖 Audio Bot "

                f"v{MODEL_VERSION}"

            )

        await safe_edit(

            status_message,

            message,

        )


    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

        if (

            audio_path

            and

            audio_path.exists()

        ):

            try:

                audio_path.unlink()

            except Exception as cleanup_error:

                print(

                    "Cleanup error: "

                    f"{cleanup_error}"

                )


# ============================================================
# HEALTH SERVER FOR KOYEB
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path in (

            "/",

            "/health",

            "/healthz",

        ):

            body = (

                f'{{"status":"ok",'

                f'"version":"'

                f'{MODEL_VERSION}",'

                f'"service":'

                f'"telegram-youtube-audio-bot"}}'

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

        ("0.0.0.0", PORT),

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


# ============================================================
# STARTUP CLEANUP
# ============================================================

def cleanup_download_directory():

    if not DOWNLOAD_DIR.exists():

        return

    for item in DOWNLOAD_DIR.iterdir():

        try:

            if (

                item.is_file()

                or

                item.is_symlink()

            ):

                item.unlink()

            elif item.is_dir():

                shutil.rmtree(item)

        except Exception as error:

            print(

                f"Startup cleanup error: "

                f"{error}"

            )


# ============================================================
# MAIN APPLICATION
# ============================================================

def main():

    if not BOT_TOKEN:

        raise ValueError(

            "BOT_TOKEN environment variable "

            "is not set."

        )

    cleanup_download_directory()

    start_health_server()

    app = (
        ApplicationBuilder()

        .token(BOT_TOKEN)

        .build()
    )

    # Commands.
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

    # Greetings.
    app.add_handler(

        MessageHandler(

            filters.Regex(

                re.compile(

                    r"^\s*"

                    r"(?:hey|hello|hii|hi)"

                    r"\s*[!.]?\s*$",

                    re.I,

                )

            ),

            greeting,

        )

    )

    # YouTube links.
    app.add_handler(

        MessageHandler(

            filters.TEXT

            &

            ~filters.COMMAND,

            handle_link,

        )

    )

    print(

        f"🤖 Audio Bot "

        f"v{MODEL_VERSION} "

        "is running..."

    )

    # Start Telegram polling.
    app.run_polling(

        drop_pending_updates=False,

        allowed_updates=Update.ALL_TYPES,

    )


if __name__ == "__main__":

    main()