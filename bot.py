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

from spotify import (
    search_spotify,
)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_VERSION = "2.2.0"

BOT_TOKEN = os.getenv(
    "BOT_TOKEN"
)

PORT = int(
    os.getenv(
        "PORT",
        "8080",
    )
)

DOWNLOAD_DIR = Path(
    os.getenv(
        "DOWNLOAD_DIR",
        "/app/downloads",
    )
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

MAX_CONCURRENT_DOWNLOADS = max(
    1,
    int(
        os.getenv(
            "MAX_CONCURRENT_DOWNLOADS",
            "1",
        )
    ),
)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)

SPOTIFY_RESULT_LIMIT = max(
    1,
    min(
        10,
        int(
            os.getenv(
                "SPOTIFY_SEARCH_LIMIT",
                "8",
            )
        ),
    ),
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


def is_youtube_url(
    url: str,
) -> bool:

    return bool(
        YOUTUBE_REGEX.match(
            url.strip()
        )
    )


# ============================================================
# HELPERS
# ============================================================

def format_bytes(
    size,
):

    if size is None:
        return "Unknown"

    try:

        size = float(
            size
        )

    except (
        TypeError,
        ValueError,
    ):

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

            return (
                f"{size:.1f} {unit}"
            )

        size /= 1024

    return (
        f"{size:.1f} TB"
    )


def format_duration(
    seconds,
):

    if not seconds:
        return None

    try:

        seconds = int(
            seconds
        )

    except (
        TypeError,
        ValueError,
    ):

        return None

    hours = (
        seconds // 3600
    )

    minutes = (
        seconds % 3600
    ) // 60

    secs = (
        seconds % 60
    )

    if hours:

        return (
            f"{hours}:"
            f"{minutes:02d}:"
            f"{secs:02d}"
        )

    return (
        f"{minutes}:"
        f"{secs:02d}"
    )


async def safe_edit(
    message,
    text,
):

    try:

        await message.edit_text(
            text,
            parse_mode="Markdown",
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


def cleanup_job_directory(
    job_dir,
):

    if not job_dir:
        return

    try:

        path = Path(
            job_dir
        )

        if path.exists():

            shutil.rmtree(
                path,
                ignore_errors=True,
            )

            print(
                f"Cleaned job directory: {path}"
            )

    except Exception as error:

        print(
            "Job cleanup error:",
            error,
        )


# ============================================================
# SPOTIFY DETECTION
# ============================================================

SPOTIFY_URL_REGEX = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?"
    r"(track|album|playlist|artist)/"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def parse_spotify_url(
    text: str,
):

    if not text:
        return None

    match = SPOTIFY_URL_REGEX.search(
        text.strip()
    )

    if not match:
        return None

    return (
        match.group(1).lower(),
        match.group(2),
    )


def looks_like_spotify_request(
    text: str,
) -> bool:

    if not text:
        return False

    text = text.strip()

    if not text:
        return False

    if SPOTIFY_URL_REGEX.search(
        text
    ):

        return True

    patterns = [
        r"\bspotify\s*$",
        r"\s*-\s*spotify\s*$",
        r"^\s*spotify\s+",
        r"^\s*spotify\s*-\s*",
    ]

    return any(
        re.search(
            pattern,
            text,
            re.IGNORECASE,
        )
        for pattern in patterns
    )


def clean_spotify_query_for_display(
    text: str,
) -> str:

    if not text:
        return ""

    query = text.strip()

    # spotify - song
    query = re.sub(
        r"^\s*spotify\s*-\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )

    # spotify song
    query = re.sub(
        r"^\s*spotify\s+",
        "",
        query,
        flags=re.IGNORECASE,
    )

    # song - spotify
    query = re.sub(
        r"\s*-\s*spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )

    # song spotify
    query = re.sub(
        r"\s+spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )

    return query.strip()


# ============================================================
# SPOTIFY → YOUTUBE RESOLUTION
# ============================================================

def build_youtube_search_query(
    track,
):

    title = (
        track.get("title")
        or ""
    ).strip()

    artist = (
        track.get("artist")
        or ""
    ).strip()

    if artist and artist != "Unknown Artist":

        return (
            f"{title} {artist}"
        )

    return title


def resolve_youtube_search(
    query: str,
):

    """
    Resolve a Spotify-selected track
    to a YouTube URL.

    No media is downloaded here.
    """

    if not query:

        raise DownloadError(
            "Spotify result did not contain "
            "a searchable title."
        )

    prepare_cookie_file()

    clients = [
        "mweb",
        "web_safari",
        "android_vr",
        "web_embedded",
    ]

    search_query = (
        f"ytsearch1:{query}"
    )

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
            "Spotify → YouTube search:",
            query,
            "| client:",
            client,
        )

        try:

            import yt_dlp

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                info = ydl.extract_info(
                    search_query,
                    download=False,
                )

            if not info:
                continue

            entries = (
                info.get("entries")
                or []
            )

            if not entries:
                continue

            entry = entries[0]

            video_url = (
                entry.get("webpage_url")
                or entry.get("original_url")
            )

            if not video_url:

                video_id = entry.get(
                    "id"
                )

                if video_id:

                    video_url = (
                        "https://www.youtube.com/watch?v="
                        f"{video_id}"
                    )

            if video_url:

                print(
                    "Spotify → YouTube resolved:",
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

        print(
            "All Spotify → YouTube search clients failed."
        )

    raise DownloadError(
        "Could not find the selected song on YouTube."
    )


# ============================================================
# NORMAL YOUTUBE SEARCH
# ============================================================

def resolve_normal_youtube_search(
    query: str,
):

    if not query:

        raise DownloadError(
            "Search query is empty."
        )

    return resolve_youtube_search(
        query
    )


async def handle_youtube_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    query = (
        update.message.text
        or ""
    ).strip()

    if not query:
        return

    status_message = (
        await update.message.reply_text(
            "🔎 *Searching YouTube...*\n\n"
            f"🎵 `{query[:100]}`",
            parse_mode="Markdown",
        )
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

        "Spotify search returns matching tracks. "
        "Select the song you want and the bot will "
        "find that song on YouTube before downloading "
        "it through the normal downloader.\n\n"

        "*Spotify Track URL*\n"
        "Spotify track URLs are recognized and can "
        "be processed through the same YouTube "
        "resolution flow.\n\n"

        "*Spotify Playlist / Album*\n"
        "Playlist and album URLs are recognized, "
        "but playlist/album track retrieval is not "
        "enabled yet.\n\n"

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
        "🔎 Spotify: RapidAPI search\n"
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

async def progress_callback(
    message,
    progress,
):

    try:

        if not progress:
            return

        percent = progress.get(
            "percent"
        )

        status = progress.get(
            "status",
            "Processing",
        )

        if percent is not None:

            try:

                percent = float(
                    percent
                )

            except (
                TypeError,
                ValueError,
            ):

                percent = 0

            percent = max(
                0,
                min(
                    100,
                    percent,
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
                f"`{bar}` "
                f"{percent:.0f}%\n\n"
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
        update.message.text
        or ""
    ).strip()

    # --------------------------------------------------------
    # DIRECT SPOTIFY URL
    # --------------------------------------------------------

    parsed_url = parse_spotify_url(
        original_text
    )

    if parsed_url:

        resource_type, resource_id = (
            parsed_url
        )

        # Track URLs can be resolved directly
        # through Spotify metadata if bot.py receives
        # enough metadata from the configured source.
        #
        # At the moment search_spotify returns the
        # resource information, but playlist/album
        # retrieval is intentionally not fabricated.

        if resource_type == "playlist":

            await update.message.reply_text(
                "📋 *Spotify playlist detected*\n\n"
                f"Playlist ID: `{resource_id}`\n\n"
                "The playlist link itself is valid, "
                "but this bot currently needs a Spotify "
                "playlist metadata source to retrieve "
                "the tracks.\n\n"
                "Use Spotify song search for now:\n"
                "`Apna Bana Le spotify`",
                parse_mode="Markdown",
            )

            return

        if resource_type == "album":

            await update.message.reply_text(
                "💿 *Spotify album detected*\n\n"
                f"Album ID: `{resource_id}`\n\n"
                "Album track retrieval is not enabled "
                "yet.\n\n"
                "You can search for an individual song:\n"
                "`Song Name spotify`",
                parse_mode="Markdown",
            )

            return

        if resource_type == "artist":

            await update.message.reply_text(
                "👤 *Spotify artist detected*\n\n"
                f"Artist ID: `{resource_id}`\n\n"
                "Artist catalogue retrieval is not "
                "enabled yet.\n\n"
                "Search for an individual song instead.",
                parse_mode="Markdown",
            )

            return

        if resource_type == "track":

            status_message = (
                await update.message.reply_text(
                    "🎵 *Spotify track detected*\n\n"
                    "🔎 Finding this track on YouTube...",
                    parse_mode="Markdown",
                )
            )

            try:

                result = await asyncio.to_thread(
                    search_spotify,
                    original_text,
                    SPOTIFY_RESULT_LIMIT,
                )

                results = (
                    result.get("results")
                    if isinstance(
                        result,
                        dict,
                    )
                    else []
                )

                # The search endpoint cannot resolve
                # a direct URL by itself. Ask the user
                # for text search rather than pretending
                # the URL was downloaded.
                if not results:

                    await safe_edit(
                        status_message,
                        "ℹ️ *Spotify track link recognized.*\n\n"
                        "This RapidAPI configuration does not "
                        "return track metadata from a direct "
                        "Spotify URL.\n\n"
                        "Search the song instead:\n"
                        "`Song Name spotify`",
                    )

                    return

            except Exception as error:

                print(
                    "Spotify URL error:",
                    type(error).__name__,
                    str(error),
                )

                await safe_edit(
                    status_message,
                    "❌ *Could not process the Spotify track.*\n\n"
                    "Try searching by song name instead.",
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

    status_message = (
        await update.message.reply_text(
            "🔎 *Searching Spotify...*\n\n"
            f"🎵 `{query[:100]}`",
            parse_mode="Markdown",
        )
    )

    try:

        result = await asyncio.to_thread(
            search_spotify,
            original_text,
            SPOTIFY_RESULT_LIMIT,
        )

        results = (
            result.get("results")
            if isinstance(
                result,
                dict,
            )
            else None
        )

        if not results:

            await safe_edit(
                status_message,
                "❌ *No Spotify results found.*\n\n"
                "Try another song title or include "
                "the artist name.",
            )

            return

        # ----------------------------------------------------
        # Store results per user.
        # ----------------------------------------------------

        context.user_data[
            "spotify_results"
        ] = results

        keyboard = []

        for index, track in enumerate(
            results
        ):

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
                    callback_data=(
                        "spotify_cancel"
                    ),
                )
            ]
        )

        await safe_edit(
            status_message,
            "🎵 *Spotify Results*\n\n"
            f"Found {len(results)} matching tracks.\n\n"
            "Select the song you want:",
        )

        await update.message.reply_text(
            "👇 *Choose a track:*",
            reply_markup=(
                InlineKeyboardMarkup(
                    keyboard
                )
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
            query.data.split(
                ":",
                1,
            )[1]
        )

    except (
        ValueError,
        IndexError,
    ):

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

        # ----------------------------------------------------
        # Selected track
        # ----------------------------------------------------

        await query.edit_message_text(
            "🎵 *Selected track*\n\n"
            f"🎧 {str(title)[:100]}\n"
            f"👤 {str(artist)[:100]}\n\n"
            "🔎 *Finding this track on YouTube...*",
            parse_mode="Markdown",
        )

        # ----------------------------------------------------
        # Resolve Spotify metadata → YouTube
        # ----------------------------------------------------

        youtube_url = await asyncio.to_thread(
            resolve_youtube_search,
            search_query,
        )

        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

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

        # ====================================================
        # QUEUE
        # ====================================================

        if DOWNLOAD_SEMAPHORE.locked():

            await safe_edit(
                status_message,
                "⏳ *You're in the queue...*\n\n"
                "Another download is currently "
                "being processed.\n\n"
                "I'll start yours automatically.",
            )

        async with DOWNLOAD_SEMAPHORE:

            # ------------------------------------------------
            # Better status for Spotify downloads.
            # ------------------------------------------------

            if spotify_track:

                selected_title = (
                    spotify_track.get(
                        "title"
                    )
                    or "Selected track"
                )

                selected_artist = (
                    spotify_track.get(
                        "artist"
                    )
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
                    lambda progress:
                    progress_callback(
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
            result.get(
                "path",
                "",
            )
        )

        job_dir = result.get(
            "job_dir"
        )

        if not audio_path.exists():

            raise DownloadError(
                "Downloaded audio file was not found."
            )

        if audio_path.stat().st_size == 0:

            raise DownloadError(
                "Downloaded audio file is empty."
            )

        # ====================================================
        # METADATA
        # ====================================================

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

        quality = (
            result.get("quality")
            or result.get("quality_text")
            or "MP3 • 192 kbps"
        )

        # ----------------------------------------------------
        # Prefer Spotify metadata when YouTube does not
        # return a useful artist.
        # ----------------------------------------------------

        if spotify_track:

            spotify_artist = (
                spotify_track.get(
                    "artist"
                )
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

        # ----------------------------------------------------
        # Prefer Spotify title if YouTube title is clearly
        # generic.
        # ----------------------------------------------------

        if spotify_track:

            spotify_title = (
                spotify_track.get(
                    "title"
                )
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

        # ====================================================
        # TELEGRAM CAPTION
        # ====================================================

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
                "🔎 Source: Spotify search → YouTube"
            )

        caption_parts.append(
            f"\n🤖 Audio Bot v{BOT_VERSION}"
        )

        caption = "\n".join(
            caption_parts
        )

        # ====================================================
        # UPLOAD
        # ====================================================

        with open(
            audio_path,
            "rb",
        ) as audio_file:

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

        await asyncio.sleep(
            2
        )

        await delete_message(
            status_message
        )

    except DownloadError as error:

        error_text = str(
            error
        )

        print(
            "DownloadError:",
            error_text,
        )

        lowered = (
            error_text.lower()
        )

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

        if job_dir:

            cleanup_job_directory(
                job_dir
            )

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
# NORMAL YOUTUBE LINK
# ============================================================

async def handle_youtube_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    original_url = (
        update.message.text
        or ""
    ).strip()

    status_message = (
        await update.message.reply_text(
            "🔍 *Analyzing YouTube link...*\n\n"
            "Please wait...",
            parse_mode="Markdown",
        )
    )

    await process_audio_download(
        update=update,
        context=context,
        original_url=original_url,
        source_message=status_message,
        spotify_track=None,
    )


# ============================================================
# HANDLE TEXT
# ============================================================

async def handle_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    # --------------------------------------------------------
    # YouTube URL
    # --------------------------------------------------------

    if is_youtube_url(
        text
    ):

        await handle_youtube_link(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # Spotify
    # --------------------------------------------------------

    if looks_like_spotify_request(
        text
    ):

        await handle_spotify_search(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # Normal YouTube search
    # --------------------------------------------------------

    await handle_youtube_search(
        update,
        context,
    )


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(
        self
    ):

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

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json",
            )

            self.send_header(
                "Content-Length",
                str(
                    len(body)
                ),
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

    cleanup_download_directory()

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
    # Spotify callbacks
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            spotify_callback,
            pattern=(
                r"^spotify_"
                r"(?:select:\d+|cancel)$"
            ),
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
    # Text messages
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_link,
        )
    )

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Startup
    # --------------------------------------------------------

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
        "🔎 Spotify Search: RapidAPI"
    )

    print(
        "🔄 YouTube clients: "
        "mweb → web_safari → "
        "android_vr → web_embedded"
    )

    print(
        f"⚡ Concurrent downloads: "
        f"{MAX_CONCURRENT_DOWNLOADS}"
    )

    print(
        "============================================"
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
