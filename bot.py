import os
import re
import asyncio
import threading
import json
import shutil
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, BotCommand
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

BOT_VERSION = "2.0.0"

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

    try:
        size = float(size)
    except (TypeError, ValueError):
        return "Unknown"

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


def format_duration(seconds):
    if not seconds:
        return None

    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return None

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


async def safe_edit(message, text):
    try:
        await message.edit_text(
            text,
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def delete_message(message):
    try:
        await message.delete()
    except Exception:
        pass


def cleanup_file(path):
    try:
        if path and Path(path).exists():
            Path(path).unlink()
    except Exception as error:
        print(f"Cleanup error: {error}")


# ============================================================
# COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        "🎵 *Audio Bot*\n\n"
        "Send me a YouTube or YouTube Music link "
        "and I'll convert it to an audio file.\n\n"

        "✨ *Supported*\n"
        "• YouTube\n"
        "• YouTube Music\n"
        "• YouTube Shorts\n"
        "• High-quality audio\n"
        "• Metadata & artwork\n"
        "• Download progress\n"
        "• Automatic cleanup\n"
        "• No account/login required\n\n"

        "📎 *Just send a YouTube link to begin.*\n\n"

        f"🤖 Audio Bot v{BOT_VERSION}",
        parse_mode="Markdown",
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        "🎵 *Audio Bot Help*\n\n"

        "*Send a YouTube URL*\n"
        "`https://youtube.com/watch?v=...`\n\n"

        "*YouTube Music*\n"
        "`https://music.youtube.com/watch?v=...`\n\n"

        "*Short URL*\n"
        "`https://youtu.be/...`\n\n"

        "*YouTube Shorts*\n"
        "`https://youtube.com/shorts/...`\n\n"

        "The bot automatically selects the best "
        "available audio source and converts it "
        "to MP3 using yt-dlp + FFmpeg.\n\n"

        "⚠️ Only download content you have permission "
        "to download.\n\n"

        f"🤖 Version {BOT_VERSION}",
        parse_mode="Markdown",
    )


async def version_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        f"🤖 *Audio Bot v{BOT_VERSION}*\n\n"

        "🎧 Engine: yt-dlp + FFmpeg\n"
        "🔐 PO Tokens: BGUTIL provider\n"
        "🎵 Output: MP3\n"
        "💿 Quality: 192 kbps\n"
        "🖼 Artwork: Enabled\n"
        "🏷 Metadata: Enabled\n"
        "🧹 Cleanup: Automatic\n"
        f"⚡ Concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}",
        parse_mode="Markdown",
    )


async def greeting(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        "👋 Hey!\n\n"
        "Send me a YouTube or YouTube Music link "
        "and I'll convert it to MP3. 🎵"
    )


# ============================================================
# PROGRESS CALLBACK
# ============================================================

async def progress_callback(
    message,
    progress,
):

    try:

        if not progress:
            return

        percent = progress.get("percent")

        status = progress.get(
            "status",
            "Processing",
        )

        if percent is not None:

            try:
                percent = float(percent)
            except (TypeError, ValueError):
                percent = 0

            percent = max(
                0,
                min(100, percent),
            )

            filled = int(percent // 10)

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

    # --------------------------------------------------------
    # Validate URL
    # --------------------------------------------------------

    if not is_youtube_url(original_url):

        await update.message.reply_text(
            "❌ *Invalid YouTube link*\n\n"
            "Please send a valid YouTube or "
            "YouTube Music URL.\n\n"
            "Example:\n"
            "`https://youtube.com/watch?v=...`",
            parse_mode="Markdown",
        )

        return

    # --------------------------------------------------------
    # Initial status
    # --------------------------------------------------------

    status_message = await update.message.reply_text(
        "🔍 *Analyzing YouTube link...*\n\n"
        "Please wait...",
        parse_mode="Markdown",
    )

    audio_path = None

    try:

        # ----------------------------------------------------
        # Queue
        # ----------------------------------------------------

        if DOWNLOAD_SEMAPHORE.locked():

            await safe_edit(
                status_message,
                "⏳ *You're in the queue...*\n\n"
                "Another download is currently being processed.\n"
                "I'll start yours automatically.",
            )

        async with DOWNLOAD_SEMAPHORE:

            # ------------------------------------------------
            # Download
            # ------------------------------------------------

            await safe_edit(
                status_message,
                "🔎 *Finding the best available audio...*\n\n"
                "Connecting to YouTube...",
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

        # ----------------------------------------------------
        # Validate result
        # ----------------------------------------------------

        audio_path = Path(
            result.get("path", "")
        )

        if not audio_path.exists():

            raise DownloadError(
                "Downloaded audio file was not found."
            )

        if audio_path.stat().st_size == 0:

            raise DownloadError(
                "Downloaded audio file is empty."
            )

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        title = (
            result.get("title")
            or "Audio"
        )

        artist = (
            result.get("artist")
            or result.get("uploader")
            or ""
        )

        duration = result.get(
            "duration"
        )

        filename = (
            result.get("filename")
            or audio_path.name
        )

        file_size = audio_path.stat().st_size

        # ----------------------------------------------------
        # Upload status
        # ----------------------------------------------------

        await safe_edit(
            status_message,
            "📤 *Uploading audio...*\n\n"
            f"🎵 {str(title)[:100]}\n"
            f"📦 {format_bytes(file_size)}",
        )

        # ----------------------------------------------------
        # Caption
        # ----------------------------------------------------

        caption_parts = [
            f"🎵 {str(title)[:300]}"
        ]

        if artist:

            caption_parts.append(
                f"👤 {str(artist)[:150]}"
            )

        formatted_duration = format_duration(
            duration
        )

        if formatted_duration:

            caption_parts.append(
                f"⏱️ {formatted_duration}"
            )

        caption_parts.append(
            f"\n🤖 Audio Bot v{BOT_VERSION}"
        )

        caption = "\n".join(
            caption_parts
        )

        # ----------------------------------------------------
        # Send audio
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Complete
        # ----------------------------------------------------

        await safe_edit(
            status_message,
            "✅ *Download complete!*\n\n"
            f"🎵 {str(title)[:100]}\n"
            f"📦 {format_bytes(file_size)}",
        )

        await asyncio.sleep(2)

        await delete_message(
            status_message
        )

    # ========================================================
    # ERROR HANDLING
    # ========================================================

    except DownloadError as error:

        error_text = str(error)

        print(
            "DownloadError:",
            error_text,
        )

        # Keep technical details out of the user-facing
        # message when possible.

        lowered = error_text.lower()

        if "private" in lowered:

            message = (
                "🔒 *Private video*\n\n"
                "This video cannot be downloaded because "
                "it is private or unavailable publicly."
            )

        elif "age" in lowered:

            message = (
                "🔞 *Age-restricted video*\n\n"
                "This video requires access that the bot "
                "cannot provide."
            )

        elif "sign in" in lowered or "bot" in lowered:

            message = (
                "🛡️ *YouTube verification required*\n\n"
                "YouTube is currently requiring additional "
                "verification for this video.\n\n"
                "Please try another video or try again later."
            )

        elif "403" in lowered:

            message = (
                "⚠️ *YouTube temporarily rejected the request.*\n\n"
                "Please try again in a few minutes."
            )

        elif "too large" in lowered:

            message = (
                "📦 *File too large*\n\n"
                "The resulting audio file is larger than "
                "the bot's configured upload limit."
            )

        elif "duration" in lowered:

            message = (
                "⏱️ *Video is too long*\n\n"
                "This bot has a maximum duration limit."
            )

        else:

            message = (
                "❌ *Download failed*\n\n"
                "The video may be unavailable, restricted, "
                "unsupported, or temporarily blocked.\n\n"
                "Please try another YouTube link."
            )

        await safe_edit(
            status_message,
            message,
        )

    except asyncio.TimeoutError:

        print(
            "Download timed out."
        )

        await safe_edit(
            status_message,
            "⏱️ *Download timed out*\n\n"
            "YouTube took too long to respond.\n"
            "Please try again.",
        )

    except Exception as error:

        print(
            "Unexpected processing error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(
            status_message,
            "❌ *Something went wrong*\n\n"
            "Please try the link again later.",
        )

    finally:

        # ----------------------------------------------------
        # Always cleanup generated audio
        # ----------------------------------------------------

        if audio_path:

            cleanup_file(
                audio_path
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
                    "po_token_provider": "bgutil",
                }
            ).encode()

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
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
# STARTUP CLEANUP
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

                shutil.rmtree(
                    item
                )

        except Exception as error:

            print(
                "Startup cleanup error:",
                error,
            )


# ============================================================
# BOT COMMAND REGISTRATION
# ============================================================

async def post_init(
    application,
):

    await application.bot.set_my_commands(
        [
            BotCommand(
                "start",
                "Start the bot",
            ),
            BotCommand(
                "help",
                "How to use the bot",
            ),
            BotCommand(
                "version",
                "Show bot version",
            ),
        ]
    )

    print(
        "Telegram bot commands registered."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    error = context.error

    print(
        "Telegram update error:",
        type(error).__name__,
        str(error),
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    # Clean old files before starting.
    cleanup_download_directory()

    # Start Render health endpoint.
    start_health_server()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Greetings
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # All non-command text
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_link,
        )
    )

    # --------------------------------------------------------
    # Error handler
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    print(
        "============================================"
    )

    print(
        f"🤖 Audio Bot v{BOT_VERSION}"
    )

    print(
        "🎧 Engine: yt-dlp + FFmpeg"
    )

    print(
        "🔐 PO Tokens: BGUTIL"
    )

    print(
        f"⚡ Concurrent downloads: "
        f"{MAX_CONCURRENT_DOWNLOADS}"
    )

    print(
        "============================================"
    )

    # IMPORTANT:
    # Only ONE running instance of this bot should
    # use the same BOT_TOKEN with polling.

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()