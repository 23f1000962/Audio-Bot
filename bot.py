import os
import re
import asyncio
import shutil
import time
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.error import BadRequest
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

MODEL_VERSION = "0.2.0"
BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Telegram's cloud Bot API currently has a practical 50 MB upload ceiling for
# bot media. Keep a safety margin so headers/transport do not push us over it.
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
SAFE_UPLOAD_LIMIT = 48 * 1024 * 1024

YOUTUBE_REGEX = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/\S+$",
    re.IGNORECASE,
)


def format_bytes(size):
    if not size:
        return "Unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024


def is_youtube_url(text):
    """Accept normal YouTube URLs, short links, query parameters and paths."""
    if not text:
        return False
    return bool(YOUTUBE_REGEX.match(text.strip()))


async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🎵 Audio Bot v{MODEL_VERSION}\n\n"
        "Send me a YouTube video link and I'll extract the audio as an MP3.\n\n"
        "Please only download content you own or are authorized to download."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Deployment/version health check. This makes it immediately obvious which
    # code version is answering after a redeploy.
    if re.fullmatch(r"(hey|hello|hii|hi|helo|hlo)[!. ]*", text, re.IGNORECASE):
        await update.message.reply_text(
            f"👋 Hey! Audio Bot v{MODEL_VERSION} is online and running."
        )
        return

    if not is_youtube_url(text):
        await update.message.reply_text(
            "❌ Please send a valid YouTube link.\n\n"
            f"🤖 Running model version: v{MODEL_VERSION}"
        )
        return

    await process_youtube(update, text)


async def process_youtube(update: Update, url: str):
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
            f"⬇️ Downloading...  •  v{MODEL_VERSION}\n"
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
        output_template = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")

        ydl_opts = {
            # Download the best available audio stream. There is deliberately
            # no filesize cap here; the final Telegram upload is checked after
            # conversion.
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            # YouTube's current JS challenges are handled by yt-dlp-ejs plus
            # the Node runtime installed in the container.
            "js_runtimes": {"node": {}},
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info["id"]
            title = info.get("title", "audio")
            duration = info.get("duration") or 0
            return DOWNLOAD_DIR / f"{video_id}.mp3", title, duration

    audio_path = None

    try:
        await safe_edit(
            status_message,
            f"⬇️ Starting download...\n🤖 Audio Bot v{MODEL_VERSION}",
        )
        audio_path, title, duration = await asyncio.to_thread(download_audio)

        if not audio_path.exists():
            raise FileNotFoundError("The audio file was not created.")

        file_size = audio_path.stat().st_size

        if file_size > SAFE_UPLOAD_LIMIT:
            await safe_edit(
                status_message,
                "⚠️ The extracted MP3 is too large for Telegram's bot upload "
                "limit.\n\n"
                f"Size: {format_bytes(file_size)}\n"
                f"Safe limit: {format_bytes(SAFE_UPLOAD_LIMIT)}\n\n"
                "Try a shorter video.",
            )
            return

        await safe_edit(
            status_message,
            f"📤 Uploading {format_bytes(file_size)}...\n"
            f"🤖 Audio Bot v{MODEL_VERSION}",
        )

        safe_title = re.sub(r"[\x00-\x1f\x7f]", "", title).strip()[:64] or "audio"

        with open(audio_path, "rb") as audio:
            await update.message.reply_audio(
                audio=audio,
                title=safe_title,
                filename=f"{safe_title[:50]}.mp3",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=30,
                pool_timeout=30,
            )

        await safe_edit(
            status_message,
            f"✅ Done!\n🤖 Audio Bot v{MODEL_VERSION}",
        )
        await asyncio.sleep(2)
        try:
            await status_message.delete()
        except Exception:
            pass

    except BadRequest as error:
        print(f"Telegram upload error: {error}")
        await safe_edit(
            status_message,
            "❌ Telegram rejected the media upload.\n"
            "The file may be beyond Telegram's bot upload limit.\n\n"
            f"🤖 Audio Bot v{MODEL_VERSION}",
        )
    except Exception as error:
        print(f"Processing error ({type(error).__name__}): {error}")
        await safe_edit(
            status_message,
            "❌ I couldn't process this YouTube link.\n\n"
            "Possible causes: YouTube availability, an unsupported video, "
            "a temporary downloader error, or a Telegram upload limit.\n\n"
            f"🤖 Audio Bot v{MODEL_VERSION}",
        )
    finally:
        if audio_path and audio_path.exists():
            try:
                audio_path.unlink()
            except Exception as cleanup_error:
                print(f"Cleanup error: {cleanup_error}")


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
            print(f"Startup cleanup error: {error}")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set.")

    cleanup_download_directory()

    # Large uploads need longer HTTPX timeouts than the defaults.
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=180,
        write_timeout=180,
        pool_timeout=30,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    print(f"🤖 Audio Bot v{MODEL_VERSION} is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
