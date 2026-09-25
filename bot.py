import os
import re
import json
import asyncio
import threading
import shutil
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from downloader import (
    download_audio,
    DownloadError,
    ProgressTracker,
    build_ydl_options,
    prepare_cookie_file,
)
from spotify import search_spotify


# ============================================================
# CONFIGURATION
# ============================================================

BOT_VERSION = "2.3.0"

BOT_TOKEN = os.getenv("BOT_TOKEN")

PORT = int(os.getenv("PORT", "8080"))

DOWNLOAD_DIR = Path(
    os.getenv("DOWNLOAD_DIR", "/app/downloads")
)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = max(
    1,
    int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1")),
)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)

SPOTIFY_RESULT_LIMIT = max(
    1,
    min(10, int(os.getenv("SPOTIFY_SEARCH_LIMIT", "8"))),
)


# ============================================================
# URL / TEXT HELPERS
# ============================================================

YOUTUBE_REGEX = re.compile(
    r"^(?:https?://)?"
    r"(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com|youtu\.be)/.+",
    re.IGNORECASE,
)

SPOTIFY_URL_REGEX = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?"
    r"(track|album|playlist|artist)/"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_REGEX.match((url or "").strip()))


def parse_spotify_url(text: str):
    if not text:
        return None

    match = SPOTIFY_URL_REGEX.search(text.strip())

    if not match:
        return None

    return (
        match.group(1).lower(),
        match.group(2),
    )


def looks_like_spotify_request(text: str) -> bool:
    if not text:
        return False

    value = text.strip()

    if not value:
        return False

    if SPOTIFY_URL_REGEX.search(value):
        return True

    return any(
        re.search(pattern, value, re.IGNORECASE)
        for pattern in (
            r"\bspotify\s*$",
            r"\s*-\s*spotify\s*$",
            r"^\s*spotify\s+",
            r"^\s*spotify\s*-\s*",
        )
    )


def clean_spotify_query_for_display(text: str) -> str:
    if not text:
        return ""

    query = text.strip()

    if SPOTIFY_URL_REGEX.search(query):
        return query

    query = re.sub(
        r"^\s*spotify\s*-\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"^\s*spotify\s+",
        "",
        query,
        count=1,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\s*-\s*spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\s+spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )

    return query.strip()


def format_bytes(size):
    if size is None:
        return "Unknown"

    try:
        size = float(size)
    except (TypeError, ValueError):
        return "Unknown"

    for unit in ("B", "KB", "MB", "GB", "TB"):
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


def cleanup_job_directory(job_dir):
    if not job_dir:
        return

    try:
        path = Path(job_dir)

        if path.exists():
            shutil.rmtree(
                path,
                ignore_errors=True,
            )
            print(f"Cleaned job directory: {path}")

    except Exception as error:
        print("Job cleanup error:", error)


# ============================================================
# SPOTIFY -> YOUTUBE
# ============================================================

def build_youtube_search_query(track):
    title = str(
        track.get("title") or ""
    ).strip()

    artist = str(
        track.get("artist") or ""
    ).strip()

    if artist and artist.lower() != "unknown artist":
        return f"{title} {artist}"

    return title


def resolve_youtube_search(query: str):
    """
    Resolve a search query to a YouTube URL.
    No media is downloaded here.
    """

    if not query:
        raise DownloadError(
            "Spotify result did not contain a searchable title."
        )

    prepare_cookie_file()

    clients = [
        "mweb",
        "web_safari",
        "android_vr",
        "web_embedded",
    ]

    search_query = f"ytsearch1:{query}"
    last_error = None

    for client in clients:
        tracker = ProgressTracker()

        options = build_ydl_options(
            output_dir=DOWNLOAD_DIR,
            progress_tracker=tracker,
            player_client=client,
        )

        options.update(
            {
                "skip_download": True,
                "extract_flat": True,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }
        )

        print(
            "YouTube search:",
            query,
            "| client:",
            client,
        )

        try:
            import yt_dlp

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(
                    search_query,
                    download=False,
                )

            if not info:
                continue

            entries = info.get("entries") or []

            if not entries:
                continue

            entry = entries[0]

            video_url = (
                entry.get("webpage_url")
                or entry.get("original_url")
            )

            if not video_url:
                video_id = entry.get("id")

                if video_id:
                    video_url = (
                        "https://www.youtube.com/watch?v="
                        f"{video_id}"
                    )

            if video_url:
                print(
                    "YouTube resolved:",
                    video_url,
                )
                return video_url

        except Exception as error:
            last_error = error

            print(
                "YouTube search client failed:",
                client,
                type(error).__name__,
                str(error),
            )

    if last_error:
        print("All YouTube search clients failed.")

    raise DownloadError(
        "Could not find the selected song on YouTube."
    )


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "🎵 *Audio Bot*\n\n"
        "Send me a YouTube or YouTube Music link "
        "and I'll convert it to audio.\n\n"
        "You can also search normally by sending "
        "a song name.\n\n"
        "✨ *Supported*\n"
        "• YouTube\n"
        "• YouTube Music\n"
        "• YouTube Shorts\n"
        "• Normal YouTube song search\n"
        "• Spotify song search\n"
        "• Spotify track links\n"
        "• High-quality audio\n"
        "• Metadata & artwork\n"
        "• Download progress\n"
        "• Automatic cleanup\n\n"
        "🎧 *Spotify search examples*\n"
        "`Apna Bana Le spotify`\n"
        "`spotify Apna Bana Le`\n"
        "`Apna Bana Le - spotify`\n\n"
        "🔎 *Normal search*\n"
        "`Apna Bana Le`\n"
        "`Apna Bana Le Arijit Singh`\n\n"
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
        "*YouTube URL*\n"
        "`https://youtube.com/watch?v=...`\n\n"
        "*YouTube Music*\n"
        "`https://music.youtube.com/watch?v=...`\n\n"
        "*YouTube Shorts*\n"
        "`https://youtube.com/shorts/...`\n\n"
        "*Normal song search*\n"
        "`Apna Bana Le`\n"
        "`Apna Bana Le Arijit Singh`\n\n"
        "*Spotify Search*\n"
        "`Apna Bana Le spotify`\n"
        "`spotify Apna Bana Le`\n"
        "`Apna Bana Le - spotify`\n\n"
        "Spotify search returns relevant tracks. "
        "Select the song and the bot will find it "
        "on YouTube before using the normal downloader.\n\n"
        "*Spotify Track URL*\n"
        "Direct Spotify track URLs are resolved through "
        "Spotify metadata and then sent through the same "
        "YouTube resolution/download flow.\n\n"
        "*Spotify Album / Playlist / Artist*\n"
        "These links are recognized, but this bot does "
        "not use Spotify audio-download endpoints.\n\n"
        "🎧 Output: MP3\n"
        "🖼 Artwork: Enabled\n"
        "🏷 Metadata: Enabled\n"
        "🧹 Automatic cleanup\n\n"
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
        "🔎 Spotify: RapidAPI metadata/search\n"
        "▶️ YouTube: Search + Download\n\n"
        f"⚡ Concurrent downloads: "
        f"{MAX_CONCURRENT_DOWNLOADS}",
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
        "Send me a YouTube link or search for "
        "a song by name. 🎵",
        parse_mode="Markdown",
    )


# ============================================================
# PROGRESS
# ============================================================

async def progress_callback(message, progress):
    try:
        if not progress:
            return

        percent = progress.get("percent")
        status = progress.get("status", "Processing")

        if percent is not None:
            try:
                percent = float(percent)
            except (TypeError, ValueError):
                percent = 0

            percent = max(0, min(100, percent))

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
# SPOTIFY SEARCH
# ============================================================

async def handle_spotify_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    original_text = (
        update.message.text or ""
    ).strip()

    parsed_url = parse_spotify_url(
        original_text
    )

    # --------------------------------------------------------
    # DIRECT SPOTIFY TRACK
    # --------------------------------------------------------

    if parsed_url:
        resource_type, resource_id = parsed_url

        if resource_type == "track":
            status_message = await update.message.reply_text(
                "🎵 *Spotify track detected*\n\n"
                "🔎 Reading track metadata...",
                parse_mode="Markdown",
            )

            try:
                result = await asyncio.to_thread(
                    search_spotify,
                    original_text,
                    SPOTIFY_RESULT_LIMIT,
                )

                results = (
                    result.get("results")
                    if isinstance(result, dict)
                    else []
                )

                if not results:
                    await safe_edit(
                        status_message,
                        "❌ *Could not resolve this Spotify track.*\n\n"
                        "Spotify metadata was found, but no "
                        "matching track was returned by the "
                        "configured search API.",
                    )
                    return

                # spotify.py puts an exact Spotify-ID match
                # first when RapidAPI exposes that ID.
                track = results[0]

                title = (
                    track.get("title")
                    or "Unknown Track"
                )
                artist = (
                    track.get("artist")
                    or "Unknown Artist"
                )

                search_query = build_youtube_search_query(
                    track
                )

                await safe_edit(
                    status_message,
                    "🎵 *Spotify track resolved*\n\n"
                    f"🎧 {str(title)[:100]}\n"
                    f"👤 {str(artist)[:100]}\n\n"
                    "🔎 *Finding the track on YouTube...*",
                )

                youtube_url = await asyncio.to_thread(
                    resolve_youtube_search,
                    search_query,
                )

                await safe_edit(
                    status_message,
                    "⬇️ *Track found on YouTube*\n\n"
                    f"🎧 {str(title)[:100]}\n"
                    f"👤 {str(artist)[:100]}\n\n"
                    "⬇️ *Downloading audio...*",
                )

                await process_audio_download(
                    update=update,
                    context=context,
                    original_url=youtube_url,
                    source_message=status_message,
                    spotify_track=track,
                )

                return

            except DownloadError as error:
                print(
                    "Spotify direct-track DownloadError:",
                    str(error),
                )

                await safe_edit(
                    status_message,
                    "❌ *Could not find this Spotify track on YouTube.*\n\n"
                    "Try the song-name Spotify search instead.",
                )
                return

            except Exception as error:
                print(
                    "Spotify direct-track error:",
                    type(error).__name__,
                    str(error),
                )

                await safe_edit(
                    status_message,
                    "❌ *Could not process this Spotify track.*\n\n"
                    "The Spotify metadata/search service may "
                    "be temporarily unavailable.",
                )
                return

        # ----------------------------------------------------
        # Album / playlist / artist
        # ----------------------------------------------------

        try:
            result = await asyncio.to_thread(
                search_spotify,
                original_text,
                SPOTIFY_RESULT_LIMIT,
            )

            resource_title = (
                result.get("query")
                if isinstance(result, dict)
                else ""
            )

        except Exception as error:
            print(
                "Spotify resource metadata error:",
                type(error).__name__,
                str(error),
            )
            resource_title = ""

        labels = {
            "album": "💿 Spotify album",
            "playlist": "📋 Spotify playlist",
            "artist": "👤 Spotify artist",
        }

        label = labels.get(
            resource_type,
            "🎵 Spotify resource",
        )

        await update.message.reply_text(
            f"{label} detected.\n\n"
            f"Title: {str(resource_title or resource_id)[:150]}\n\n"
            "This resource type is recognized, but this bot "
            "does not use Spotify audio-download endpoints.\n\n"
            "For an individual song, send its Spotify track "
            "link or use:\n"
            "`Song Name spotify`",
            parse_mode="Markdown",
        )

        return

    # --------------------------------------------------------
    # TEXT SEARCH
    # --------------------------------------------------------

    query = clean_spotify_query_for_display(
        original_text
    )

    if not query:
        await update.message.reply_text(
            "❌ *Spotify search query is empty.*\n\n"
            "Example:\n"
            "`Apna Bana Le spotify`",
            parse_mode="Markdown",
        )
        return

    status_message = await update.message.reply_text(
        "🔎 *Searching Spotify...*\n\n"
        f"🎵 `{query[:100]}`",
        parse_mode="Markdown",
    )

    try:
        result = await asyncio.to_thread(
            search_spotify,
            original_text,
            SPOTIFY_RESULT_LIMIT,
        )

        results = (
            result.get("results")
            if isinstance(result, dict)
            else None
        )

        if not results:
            await safe_edit(
                status_message,
                "❌ *No relevant Spotify tracks found.*\n\n"
                "Try another title or include the artist name.",
            )
            return

        context.user_data[
            "spotify_results"
        ] = results

        keyboard = []

        for index, track in enumerate(results):
            title = (
                track.get("title")
                or "Unknown Track"
            )

            artist = (
                track.get("artist")
                or "Unknown Artist"
            )

            duration = (
                track.get("duration")
                or ""
            )

            if duration:
                button_text = (
                    f"{index + 1}. "
                    f"{str(title)[:34]} — "
                    f"{str(artist)[:18]} "
                    f"({str(duration)[:5]})"
                )
            else:
                button_text = (
                    f"{index + 1}. "
                    f"{str(title)[:38]} — "
                    f"{str(artist)[:20]}"
                )

            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text[:64],
                        callback_data=(
                            f"spotify_select:{index}"
                        ),
                    )
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="spotify_cancel",
                )
            ]
        )

        await safe_edit(
            status_message,
            "🎵 *Spotify Results*\n\n"
            f"Found {len(results)} relevant tracks.\n\n"
            "Select the song you want:",
        )

        await update.message.reply_text(
            "👇 *Choose a track:*",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
            parse_mode="Markdown",
        )

    except Exception as error:
        print(
            "Spotify search error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(
            status_message,
            "❌ *Spotify search failed.*\n\n"
            "Please try again or use a different "
            "song title.",
        )


# ============================================================
# SPOTIFY CALLBACK
# ============================================================

async def spotify_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    if query.data == "spotify_cancel":
        try:
            await query.edit_message_text(
                "❌ Spotify selection cancelled."
            )
        except Exception:
            pass

        context.user_data.pop(
            "spotify_results",
            None,
        )
        return

    if not query.data.startswith(
        "spotify_select:"
    ):
        return

    try:
        index = int(
            query.data.split(":", 1)[1]
        )
    except (ValueError, IndexError):
        await query.answer(
            "Invalid selection.",
            show_alert=True,
        )
        return

    results = context.user_data.get(
        "spotify_results",
        [],
    )

    if (
        not results
        or index < 0
        or index >= len(results)
    ):
        await query.answer(
            "This search has expired. Search again.",
            show_alert=True,
        )
        return

    track = results[index]

    title = (
        track.get("title")
        or "Unknown Track"
    )

    artist = (
        track.get("artist")
        or ""
    )

    search_query = build_youtube_search_query(
        track
    )

    try:
        await query.edit_message_text(
            "🎵 *Selected track*\n\n"
            f"🎧 {str(title)[:100]}\n"
            f"👤 {str(artist)[:100]}\n\n"
            "🔎 *Finding this track on YouTube...*",
            parse_mode="Markdown",
        )

        youtube_url = await asyncio.to_thread(
            resolve_youtube_search,
            search_query,
        )

        await safe_edit(
            query.message,
            "⬇️ *Track found on YouTube.*\n\n"
            f"🎵 {str(title)[:100]}\n"
            f"👤 {str(artist)[:100]}\n\n"
            "⬇️ *Downloading audio...*",
        )

        await process_audio_download(
            update=update,
            context=context,
            original_url=youtube_url,
            source_message=query.message,
            spotify_track=track,
        )

    except DownloadError as error:
        print(
            "Spotify selection DownloadError:",
            str(error),
        )

        await safe_edit(
            query.message,
            "❌ *Could not find this song on YouTube.*\n\n"
            f"🎵 {str(title)[:100]}\n"
            f"👤 {str(artist)[:100]}\n\n"
            "Try another result or search again.",
        )

    except Exception as error:
        print(
            "Spotify callback error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(
            query.message,
            "❌ *Something went wrong.*\n\n"
            "Please try the search again.",
        )

    finally:
        context.user_data.pop(
            "spotify_results",
            None,
        )


# ============================================================
# AUDIO DOWNLOAD PROCESSOR
# ============================================================

async def process_audio_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    original_url: str,
    source_message,
    spotify_track=None,
):
    status_message = source_message
    audio_path = None
    job_dir = None

    try:
        if DOWNLOAD_SEMAPHORE.locked():
            await safe_edit(
                status_message,
                "⏳ *You're in the queue...*\n\n"
                "Another download is currently being processed.\n\n"
                "I'll start yours automatically.",
            )

        async with DOWNLOAD_SEMAPHORE:
            if spotify_track:
                selected_title = (
                    spotify_track.get("title")
                    or "Selected track"
                )
                selected_artist = (
                    spotify_track.get("artist")
                    or ""
                )

                await safe_edit(
                    status_message,
                    "⬇️ *Downloading audio...*\n\n"
                    f"🎵 {str(selected_title)[:100]}\n"
                    f"👤 {str(selected_artist)[:100]}\n\n"
                    "⚙️ Processing with yt-dlp + FFmpeg...",
                )
            else:
                await safe_edit(
                    status_message,
                    "🔎 *Finding the best available audio...*\n\n"
                    "⚙️ Processing with yt-dlp + FFmpeg...",
                )

            result = await download_audio(
                url=original_url,
                output_dir=DOWNLOAD_DIR,
                progress_callback=(
                    lambda progress: progress_callback(
                        status_message,
                        progress,
                    )
                ),
            )

        if not result:
            raise DownloadError(
                "Downloader returned no result."
            )

        audio_path = Path(
            result.get("path", "")
        )

        job_dir = result.get("job_dir")

        if not audio_path.exists():
            raise DownloadError(
                "Downloaded audio file was not found."
            )

        if audio_path.stat().st_size == 0:
            raise DownloadError(
                "Downloaded audio file is empty."
            )

        title = (
            result.get("title")
            or "Audio"
        )

        artist = (
            result.get("artist")
            or result.get("uploader")
            or ""
        )

        duration = result.get("duration")

        filename = (
            result.get("filename")
            or audio_path.name
        )

        file_size = audio_path.stat().st_size

        quality = (
            result.get("quality")
            or result.get("quality_text")
            or "MP3 • 192 kbps"
        )

        if spotify_track:
            spotify_artist = (
                spotify_track.get("artist")
                or ""
            )

            if (
                spotify_artist
                and (
                    not artist
                    or artist.lower()
                    in (
                        "unknown artist",
                        "unknown",
                    )
                )
            ):
                artist = spotify_artist

            spotify_title = (
                spotify_track.get("title")
                or ""
            )

            if (
                spotify_title
                and (
                    not title
                    or title.lower()
                    in (
                        "audio",
                        "unknown",
                        "unknown track",
                    )
                )
            ):
                title = spotify_title

        await safe_edit(
            status_message,
            "📤 *Uploading audio...*\n\n"
            f"🎵 {str(title)[:100]}\n"
            f"📦 {format_bytes(file_size)}\n"
            f"🎧 {str(quality)[:100]}",
        )

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
            f"🎧 {str(quality)[:150]}"
        )

        if spotify_track:
            caption_parts.append(
                "🔎 Source: Spotify metadata/search → YouTube"
            )

        caption_parts.append(
            f"\n🤖 Audio Bot v{BOT_VERSION}"
        )

        caption = "\n".join(caption_parts)

        with open(audio_path, "rb") as audio_file:
            await update.effective_message.reply_audio(
                audio=audio_file,
                title=str(title)[:128],
                performer=(
                    str(artist)[:64]
                    if artist
                    else None
                ),
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
            f"🎵 {str(title)[:100]}\n"
            f"📦 {format_bytes(file_size)}\n"
            f"🎧 {str(quality)[:100]}",
        )

        await asyncio.sleep(2)

        await delete_message(status_message)

    except DownloadError as error:
        error_text = str(error)

        print(
            "DownloadError:",
            error_text,
        )

        lowered = error_text.lower()

        if "private" in lowered:
            message = (
                "🔒 *Private video*\n\n"
                "This video is private or unavailable."
            )
        elif "age" in lowered:
            message = (
                "🔞 *Age-restricted video*\n\n"
                "This video requires access that the bot "
                "cannot provide."
            )
        elif (
            "rate-limit" in lowered
            or "rate limit" in lowered
            or "429" in lowered
        ):
            message = (
                "⏳ *YouTube is temporarily busy*\n\n"
                "YouTube is rate-limiting this server "
                "right now.\n\n"
                "Please try again after a short while."
            )
        elif (
            "sign in" in lowered
            or "bot" in lowered
        ):
            message = (
                "🛡️ *YouTube verification required*\n\n"
                "YouTube is currently requiring additional "
                "verification for this request.\n\n"
                "Please try again later."
            )
        elif "403" in lowered:
            message = (
                "⚠️ *YouTube temporarily rejected the request.*\n\n"
                "Please try again in a few minutes."
            )
        elif "too large" in lowered:
            message = (
                "📦 *File too large*\n\n"
                "The resulting audio file exceeds "
                "the configured upload limit."
            )
        elif "duration" in lowered:
            message = (
                "⏱️ *Video is too long*\n\n"
                "The maximum allowed duration has been exceeded."
            )
        else:
            message = (
                "❌ *Download failed*\n\n"
                "The video may be unavailable, restricted, "
                "unsupported, or temporarily blocked.\n\n"
                "Please try another link."
            )

        await safe_edit(
            status_message,
            message,
        )

    except asyncio.TimeoutError:
        print("Download timed out.")

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
        if job_dir:
            cleanup_job_directory(job_dir)
        elif audio_path:
            try:
                if audio_path.exists():
                    audio_path.unlink()
            except Exception as error:
                print(
                    "Fallback cleanup error:",
                    error,
                )


# ============================================================
# NORMAL YOUTUBE
# ============================================================

async def handle_youtube_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    original_url = (
        update.message.text or ""
    ).strip()

    status_message = await update.message.reply_text(
        "🔍 *Analyzing YouTube link...*\n\n"
        "Please wait...",
        parse_mode="Markdown",
    )

    await process_audio_download(
        update=update,
        context=context,
        original_url=original_url,
        source_message=status_message,
        spotify_track=None,
    )


def resolve_normal_youtube_search(query: str):
    if not query:
        raise DownloadError(
            "Search query is empty."
        )

    return resolve_youtube_search(query)


async def handle_youtube_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    query = (
        update.message.text or ""
    ).strip()

    if not query:
        return

    status_message = await update.message.reply_text(
        "🔎 *Searching YouTube...*\n\n"
        f"🎵 `{query[:100]}`",
        parse_mode="Markdown",
    )

    try:
        youtube_url = await asyncio.to_thread(
            resolve_normal_youtube_search,
            query,
        )

        await process_audio_download(
            update=update,
            context=context,
            original_url=youtube_url,
            source_message=status_message,
            spotify_track=None,
        )

    except DownloadError:
        await safe_edit(
            status_message,
            "❌ *No suitable YouTube result found.*\n\n"
            "Try adding the artist name.",
        )

    except Exception as error:
        print(
            "YouTube search error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(
            status_message,
            "❌ *YouTube search failed.*\n\n"
            "Please try again.",
        )


# ============================================================
# MESSAGE ROUTER
# ============================================================

async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = (
        update.message.text or ""
    ).strip()

    if not text:
        return

    if is_youtube_url(text):
        await handle_youtube_link(
            update,
            context,
        )
        return

    if looks_like_spotify_request(text):
        await handle_spotify_search(
            update,
            context,
        )
        return

    await handle_youtube_search(
        update,
        context,
    )


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

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
                    "spotify_search": "rapidapi",
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

    def log_message(self, format, *args):
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
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(
                    item,
                    ignore_errors=True,
                )
        except Exception as error:
            print(
                "Startup cleanup error:",
                error,
            )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def post_init(application):
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

    cleanup_download_directory()
    start_health_server()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

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

    application.add_handler(
        CallbackQueryHandler(
            spotify_callback,
            pattern=(
                r"^spotify_"
                r"(?:select:\d+|cancel)$"
            ),
        )
    )

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

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("============================================")
    print(f"🤖 Audio Bot v{BOT_VERSION}")
    print("🎧 Engine: yt-dlp + FFmpeg")
    print("🔐 PO Tokens: BGUTIL")
    print("🔎 Spotify Search: RapidAPI")
    print(
        "🔄 YouTube clients: "
        "mweb → web_safari → android_vr → web_embedded"
    )
    print(
        f"⚡ Concurrent downloads: "
        f"{MAX_CONCURRENT_DOWNLOADS}"
    )
    print("============================================")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
