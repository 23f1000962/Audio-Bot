import os
import re
import asyncio
import shutil
import time
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

MODEL_VERSION = "0.3.0"
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

YOUTUBE_REGEX = re.compile(
    r"^(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/.+",
    re.IGNORECASE,
)

MAX_TELEGRAM_AUDIO_BYTES = 49 * 1024 * 1024


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


async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🎵 Audio Bot v{MODEL_VERSION} is online.\n\n"
        "Send me a YouTube video link and I'll extract the audio as an MP3.\n\n"
        "Please only download content you own or are authorized to download."
    )


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 Audio Bot version: v{MODEL_VERSION}\n"
        "✅ Container is running."
    )


async def greeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Hey! Audio Bot v{MODEL_VERSION} is online and running."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not YOUTUBE_REGEX.match(url):
        await update.message.reply_text(
            f"❌ Please send a valid YouTube link.\n\n"
            f"🤖 Audio Bot v{MODEL_VERSION}"
        )
        return

    status_message = await update.message.reply_text(
        f"🔍 Checking the link...\n🤖 v{MODEL_VERSION}"
    )

    loop = asyncio.get_running_loop()
    progress_data = {"last_update": 0}

    def progress_hook(data):
        if data.get("status") != "downloading":
            return

        now = time.time()

        if now - progress_data["last_update"] < 2:
            return

        progress_data["last_update"] = now

        downloaded = data.get("downloaded_bytes", 0)

        total = (
            data.get("total_bytes")
            or data.get("total_bytes_estimate")
            or 0
        )

        speed = data.get("speed")
        percent = data.get("_percent_str", "").strip()

        text = (
            f"⬇️ Downloading... (v{MODEL_VERSION})\n"
            f"Progress: {percent or 'Calculating...'}\n"
            f"Downloaded: {format_bytes(downloaded)}"
        )

        if total:
            text += f" / {format_bytes(total)}"

        if speed:
            text += f"\nSpeed: {format_bytes(speed)}/s"

        asyncio.run_coroutine_threadsafe(
            safe_edit(status_message, text),
            loop,
        )

    def download_audio():
        output_template = str(
            DOWNLOAD_DIR / "%(id)s.%(ext)s"
        )

        ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "js_runtimes": {"node": {}},
            "progress_hooks": [progress_hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )

            video_id = info["id"]
            title = info.get("title", "audio")

            return (
                DOWNLOAD_DIR / f"{video_id}.mp3",
                title
            )

    audio_path = None

    try:
        await safe_edit(
            status_message,
            f"⬇️ Starting download...\n"
            f"🤖 v{MODEL_VERSION}",
        )

        audio_path, title = await asyncio.to_thread(
            download_audio
        )

        if not audio_path.exists():
            raise FileNotFoundError(
                "The audio file was not created."
            )

        size = audio_path.stat().st_size

        if size > MAX_TELEGRAM_AUDIO_BYTES:
            raise ValueError(
                f"Final MP3 is {format_bytes(size)}, "
                f"above the safe Telegram upload limit of "
                f"{format_bytes(MAX_TELEGRAM_AUDIO_BYTES)}."
            )

        await safe_edit(
            status_message,
            f"📤 Uploading {format_bytes(size)} "
            f"to Telegram...\n"
            f"🤖 v{MODEL_VERSION}",
        )

        safe_title = re.sub(
            r"[\r\n]+",
            " ",
            title
        ).strip()[:64] or "audio"

        with open(audio_path, "rb") as audio:
            await update.message.reply_audio(
                audio=audio,
                title=safe_title,
                filename=f"{safe_title[:50]}.mp3",
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
            and "above the safe Telegram" in str(error)
        ):
            message = (
                f"❌ The converted MP3 is too large "
                f"for Telegram.\n\n"
                f"Size: "
                f"{format_bytes(audio_path.stat().st_size) if audio_path and audio_path.exists() else 'Unknown'}\n"
                f"Safe limit: "
                f"{format_bytes(MAX_TELEGRAM_AUDIO_BYTES)}\n\n"
                f"🤖 Audio Bot v{MODEL_VERSION}"
            )
        else:
            message = (
                "❌ Sorry, I couldn't process this link.\n"
                "It may be unavailable, unsupported, "
                "blocked by YouTube, or too large "
                "for Telegram.\n\n"
                f"🤖 Audio Bot v{MODEL_VERSION}"
            )

        await safe_edit(
            status_message,
            message
        )

    finally:
        if audio_path and audio_path.exists():
            try:
                audio_path.unlink()
            except Exception as cleanup_error:
                print(
                    f"Cleanup error: "
                    f"{cleanup_error}"
                )


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
            "/healthz",
        ):

            body = (
                f'{{"status":"ok",'
                f'"version":"{MODEL_VERSION}",'
                f'"service":"telegram-youtube-audio-bot"}}'
            ).encode()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    thread.start()

    print(
        f"Health server listening on "
        f"0.0.0.0:{PORT}"
    )


def cleanup_download_directory():
    """Remove files left behind after crashes or interrupted downloads."""

    if not DOWNLOAD_DIR.exists():
        return

    for item in DOWNLOAD_DIR.iterdir():

        try:

            if item.is_file() or item.is_symlink():
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
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "version",
            version_command
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Regex(
                re.compile(
                    r"^\s*(?:hey|hello|hii|hi)\s*[!.]?\s*$",
                    re.I
                )
            ),
            greeting,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        )
    )

    print(
        f"🤖 Audio Bot v{MODEL_VERSION} "
        f"is running..."
    )

    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
