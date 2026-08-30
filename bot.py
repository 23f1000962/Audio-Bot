import os
import re
import asyncio
import shutil
import time
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

YOUTUBE_REGEX = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+",
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


async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Send me a YouTube video link and I'll extract the audio as an MP3.\n\n"
        "Please only download content you own or are authorized to download."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not YOUTUBE_REGEX.match(url):
        await update.message.reply_text("❌ Please send a valid YouTube link.")
        return

    status_message = await update.message.reply_text("🔍 Checking the link...")
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
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        speed = data.get("speed")
        percent = data.get("_percent_str", "").strip()

        text = (
            "⬇️ Downloading...\n"
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
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info["id"]
            title = info.get("title", "audio")
            return DOWNLOAD_DIR / f"{video_id}.mp3", title

    audio_path = None

    try:
        await safe_edit(status_message, "⬇️ Starting download...")
        audio_path, title = await asyncio.to_thread(download_audio)

        if not audio_path.exists():
            raise FileNotFoundError("The audio file was not created.")

        await safe_edit(status_message, "📤 Uploading audio to Telegram...")

        safe_title = title[:64]
        with open(audio_path, "rb") as audio:
            await update.message.reply_audio(
                audio=audio,
                title=safe_title,
                filename=f"{safe_title[:50]}.mp3",
            )

        await safe_edit(status_message, "✅ Done!")
        await asyncio.sleep(2)
        try:
            await status_message.delete()
        except Exception:
            pass

    except Exception as error:
        print(f"Processing error: {error}")
        await safe_edit(
            status_message,
            "❌ Sorry, I couldn't process this link. It may be unavailable, "
            "unsupported, or too large for Telegram to accept.",
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

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )

    print("🤖 Audio Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
