import os
import re
import asyncio
import threading
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from downloader import (
    download_audio,
    DownloadError,
)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_VERSION = "1.0.0"

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/app/downloads")
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)

MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")
)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)


# ============================================================
# YOUTUBE URL VALIDATION
# ============================================================

YOUTUBE_REGEX = re.compile(
    r"^(?:https?://)?"
    r"(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com|youtu\.be)/.+",
    re.IGNORECASE,
)


def is_youtube_url(url: str) -> bool:
    return bool(
        YOUTUBE_REGEX.match(
            url.strip()
        )
    )


# ============================================================
# HELPERS
# ============================================================

def format_bytes(size):
    if size is None:
        return "Unknown"

    size = float(size)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    for unit in units:

        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} TB"


async def safe_edit(
    message,
    text,
):
    try:

        await message.edit_text(
            text
        )

    except Exception:
        pass


async def delete_message(
    message,
):
    try:

        await message.delete()

    except Exception:
        pass


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🎵 *Audio Bot*\n\n"
        "Send me a YouTube or YouTube Music link "
        "and I'll extract the highest-quality available "
        "audio for you.\n\n"
        "✨ Features:\n"
        "• High-quality audio\n"
        "• YouTube Music support\n"
        "• YouTube Shorts support\n"
        "• Metadata & artwork\n"
        "• Automatic cleanup\n"
        "• Download progress\n"
        "• Smart quality handling\n\n"
        "📌 You can also use:\n"
        "`/song artist - song name`\n\n"
        f"🤖 Version {BOT_VERSION}",
        parse_mode="Markdown",
    )


# ============================================================
# /HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🎵 *Audio Bot Help*\n\n"

        "Send a YouTube URL:\n"
        "`https://youtube.com/watch?v=...`\n\n"

        "YouTube Music:\n"
        "`https://music.youtube.com/watch?v=...`\n\n"

        "Short URL:\n"
        "`https://youtu.be/...`\n\n"

        "YouTube Shorts:\n"
        "`https://youtube.com/shorts/...`\n\n"

        "Search mode:\n"
        "`/song Believer Imagine Dragons`\n\n"

        "The bot automatically selects the best "
        "available audio and processes it using "
        "FFmpeg.\n\n"

        f"🤖 Version {BOT_VERSION}",
        parse_mode="Markdown",
    )


# ============================================================
# /VERSION
# ============================================================

async def version_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        f"🤖 *Audio Bot v{BOT_VERSION}*\n\n"
        "Engine: yt-dlp + FFmpeg\n"
        "Quality: Best available\n"
        "Metadata: Enabled\n"
        "Artwork: Enabled\n"
        "Cleanup: Automatic",
        parse_mode="Markdown",
    )


# ============================================================
# GREETING
# ============================================================

async def greeting(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Hey!\n\n"
        "Send me a YouTube link and I'll "
        "download the audio for you. 🎵"
    )


# ============================================================
# PROGRESS CALLBACK
# ============================================================

async def progress_callback(
    message,
    progress,
):

    try:

        if progress is None:
            return

        percent = progress.get(
            "percent"
        )

        status = progress.get(
            "status",
            "Processing",
        )

        if percent is not None:

            percent = max(
                0,
                min(
                    100,
                    float(percent),
                ),
            )

            filled = int(
                percent // 10
            )

            bar = (
                "█" * filled
                + "░" * (10 - filled)
            )

            text = (
                f"🎵 *{status}*\n\n"
                f"`{bar}` {percent:.0f}%\n\n"
                f"🤖 v{BOT_VERSION}"
            )

        else:

            text = (
                f"⏳ *{status}...*\n\n"
                f"🤖 v{BOT_VERSION}"
            )

        await safe_edit(
            message,
            text,
        )

    except Exception:
        pass


# ============================================================
# HANDLE YOUTUBE LINK
# ============================================================

async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    original_url = (
        update.message.text
        or ""
    ).strip()

    if not is_youtube_url(
        original_url
    ):

        await update.message.reply_text(
            "❌ Please send a valid YouTube or "
            "YouTube Music URL.\n\n"
            "Example:\n"
            "https://youtube.com/watch?v=..."
        )

        return

    status_message = (
        await update.message.reply_text(
            "🔍 *Analyzing YouTube link...*\n\n"
            f"🤖 v{BOT_VERSION}",
            parse_mode="Markdown",
        )
    )

    try:

        async with DOWNLOAD_SEMAPHORE:

            await safe_edit(
                status_message,
                "🔎 *Finding the best available audio...*\n\n"
                "Please wait...",
                )

            result = await download_audio(
                url=original_url,
                output_dir=DOWNLOAD_DIR,
                progress_callback=lambda progress:
                    progress_callback(
                        status_message,
                        progress,
                    ),
            )

        if not result:

            raise DownloadError(
                "Downloader returned no result."
            )

        audio_path = Path(
            result["path"]
        )

        if not audio_path.exists():

            raise DownloadError(
                "Downloaded audio file does not exist."
            )

        title = (
            result.get(
                "title"
            )
            or "Audio"
        )

        artist = (
            result.get(
                "artist"
            )
            or result.get(
                "uploader"
            )
            or ""
        )

        duration = result.get(
            "duration"
        )

        file_size = (
            audio_path.stat().st_size
        )

        await safe_edit(
            status_message,
            "📤 *Uploading audio to Telegram...*\n\n"
            f"🎵 {title}\n"
            f"📦 {format_bytes(file_size)}\n\n"
            f"🤖 v{BOT_VERSION}",
            )

        caption_parts = [
            f"🎵 {title}",
        ]

        if artist:
            caption_parts.append(
                f"👤 {artist}"
            )

        if duration:
            try:

                minutes = int(
                    duration // 60
                )

                seconds = int(
                    duration % 60
                )

                caption_parts.append(
                    f"⏱️ {minutes}:{seconds:02d}"
                )

            except Exception:
                pass

        caption_parts.append(
            f"\n🤖 Audio Bot v{BOT_VERSION}"
        )

        caption = "\n".join(
            caption_parts
        )

        filename = (
            result.get(
                "filename"
            )
            or audio_path.name
        )

        with open(
            audio_path,
            "rb",
        ) as audio_file:

            await update.message.reply_audio(
                audio=audio_file,
                title=str(title)[:128],
                performer=str(artist)[:64]
                if artist
                else None,
                filename=filename,
                caption=caption[:1024],
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
                pool_timeout=60,
            )

        await safe_edit(
            status_message,
            "✅ *Download complete!*\n\n"
            f"🎵 {title}\n"
            f"📦 {format_bytes(file_size)}\n\n"
            f"🤖 Audio Bot v{BOT_VERSION}",
            )

        await asyncio.sleep(2)

        await delete_message(
            status_message
        )

    except DownloadError as error:

        print(
            "DownloadError:",
            str(error),
        )

        await safe_edit(
            status_message,
            "❌ *Download failed*\n\n"
            f"{str(error)[:700]}\n\n"
            f"🤖 Audio Bot v{BOT_VERSION}",
            )

    except asyncio.TimeoutError:

        await safe_edit(
            status_message,
            "⏱️ *Download timed out.*\n\n"
            "Please try again later.",
            )

    except Exception as error:

        print(
            "Unexpected processing error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(
            status_message,
            "❌ *Something went wrong.*\n\n"
            "The video may be unavailable, "
            "restricted, unsupported, or too large.\n\n"
            "Please try another YouTube link.",
            )

    finally:

        # Safety cleanup.
        try:

            if (
                "audio_path" in locals()
                and audio_path
                and audio_path.exists()
            ):

                audio_path.unlink()

        except Exception as cleanup_error:

            print(
                "Cleanup error:",
                cleanup_error,
            )


# ============================================================
# HEALTH SERVER
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

            body = json.dumps(
                {
                    "status": "ok",
                    "service": "audio-bot",
                    "version": BOT_VERSION,
                    "engine": "yt-dlp + FFmpeg",
                }
            ).encode()

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json",
            )

            self.send_header(
                "Content-Length",
                str(len(body)),
            )

            self.end_headers()

            self.wfile.write(
                body
            )

        else:

            self.send_response(
                404
            )

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
        f"Health server running on port {PORT}"
    )


# ============================================================
# CLEANUP
# ============================================================

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

                import shutil

                shutil.rmtree(
                    item
                )

        except Exception as error:

            print(
                "Startup cleanup error:",
                error,
            )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    cleanup_download_directory()

    start_health_server()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "version",
            version_command,
        )
    )

    # Greetings
    application.add_handler(
        MessageHandler(
            filters.Regex(
                re.compile(
                    r"^\s*"
                    r"(hey|hello|hii|hi)"
                    r"\s*[!.]?\s*$",
                    re.IGNORECASE,
                )
            ),
            greeting,
        )
    )

    # YouTube URLs
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_link,
        )
    )

    print(
        f"🤖 Audio Bot v{BOT_VERSION} "
        "starting..."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()