import os
import re
import asyncio
import shutil
from pathlib import Path

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
    download_spotify_song,
)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_VERSION = "2.5.0"

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Render's public web-service port.
# Do NOT manually set PORT on Render.
PORT = int(os.getenv("PORT", "8080"))


# ============================================================
# RENDER PUBLIC URL
# ============================================================

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "",
).rstrip("/")


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH",
    "telegram-webhook",
).strip("/")

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "",
)

if RENDER_EXTERNAL_URL:

    WEBHOOK_URL = (
        f"{RENDER_EXTERNAL_URL}/{WEBHOOK_PATH}"
    )

else:

    WEBHOOK_URL = ""


# ============================================================
# DOWNLOAD CONFIGURATION
# ============================================================

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

    return bool(
        YOUTUBE_REGEX.match(
            (url or "").strip()
        )
    )


def parse_spotify_url(text: str):

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

    value = text.strip()

    if not value:
        return False

    if SPOTIFY_URL_REGEX.search(value):
        return True

    return any(
        re.search(
            pattern,
            value,
            re.IGNORECASE,
        )
        for pattern in (
            r"\bspotify\s*$",
            r"\s*-\s*spotify\s*$",
            r"^\s*spotify\s+",
            r"^\s*spotify\s*-\s*",
        )
    )


def clean_spotify_query_for_display(
    text: str,
) -> str:

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

    except (
        TypeError,
        ValueError,
    ):

        return "Unknown"

    for unit in (
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ):

        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} TB"


def format_duration(seconds):

    if not seconds:
        return None

    try:

        seconds = int(seconds)

    except (
        TypeError,
        ValueError,
    ):

        return None

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    secs = seconds % 60

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

            print(
                f"Cleaned job directory: {path}"
            )

    except Exception as error:

        print(
            "Job cleanup error:",
            error,
        )


# ============================================================
# YOUTUBE SEARCH
# ============================================================

def resolve_youtube_search(
    query: str,
):

    if not query:

        raise DownloadError(
            "Search query is empty."
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
            "YouTube search:",
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
                entry.get(
                    "webpage_url"
                )
                or entry.get(
                    "original_url"
                )
            )

            if not video_url:

                video_id = entry.get(
                    "id"
                )

                if video_id:

                    video_url = (
                        "https://www.youtube.com/"
                        "watch?v="
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

        print(
            "All YouTube search clients failed."
        )

    raise DownloadError(
        "Could not find the requested "
        "song on YouTube."
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

        "Send me a YouTube or YouTube Music "
        "link and I'll convert it to audio.\n\n"

        "You can also search normally by "
        "sending a song name.\n\n"

        "✨ *Supported*\n"
        "• YouTube\n"
        "• YouTube Music\n"
        "• YouTube Shorts\n"
        "• YouTube song search\n"
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

        "🔎 *YouTube search examples*\n"

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

        "*YouTube Search*\n"
        "`Apna Bana Le`\n"
        "`Apna Bana Le Arijit Singh`\n\n"

        "*Spotify Search*\n"
        "`Apna Bana Le spotify`\n"
        "`spotify Apna Bana Le`\n"
        "`Apna Bana Le - spotify`\n\n"

        "Spotify search returns tracks from "
        "Spotify. Select a track and the bot "
        "downloads that Spotify track directly.\n\n"

        "*Spotify Track URL*\n"
        "A Spotify track URL is detected directly. "
        "The Spotify track ID is extracted and "
        "sent to the Spotify download endpoint.\n\n"

        "*Spotify Album / Playlist / Artist*\n"
        "These links are recognized, but the "
        "current implementation is focused on "
        "individual tracks.\n\n"

        "🎧 Output: MP3\n"
        "🖼 Artwork: Enabled when available\n"
        "🏷 Metadata: Enabled when available\n"
        "🧹 Automatic cleanup\n\n"

        "⚠️ Only download content you have "
        "permission to download.\n\n"

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

        "🎧 YouTube: yt-dlp + FFmpeg\n"
        "🔐 PO Tokens: BGUTIL provider\n"
        "🎵 Output: MP3\n"
        "💿 Quality: 192 kbps\n"
        "🖼 Artwork: Enabled\n"
        "🏷 Metadata: Enabled\n"
        "🧹 Cleanup: Automatic\n"
        "🔎 Spotify: RapidAPI\n"
        "🎵 Spotify: Direct download\n"
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

        "👋 *Hey! I'm ready.*\n\n"

        "🎵 Send me a YouTube or YouTube "
        "Music link to download the audio.\n\n"

        "🔎 Or simply type a song name.\n\n"

        "🎧 For Spotify, type:\n"
        "`Song Name spotify`",

        parse_mode="Markdown",
    )


# ============================================================
# YOUTUBE PROGRESS
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

                percent = float(percent)

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
        update.message.text or ""
    ).strip()

    parsed_url = parse_spotify_url(
        original_text
    )

    # --------------------------------------------------------
    # DIRECT SPOTIFY RESOURCE
    # --------------------------------------------------------

    if parsed_url:

        resource_type, resource_id = (
            parsed_url
        )

        # ----------------------------------------------------
        # DIRECT TRACK URL
        # ----------------------------------------------------

        if resource_type == "track":

            status_message = (
                await update.message.reply_text(

                    "🎵 *Spotify track detected*\n\n"
                    "🔎 Searching Spotify...",
                    
                    parse_mode="Markdown",
                )
            )

            try:

                await process_spotify_download(
                    update=update,
                    context=context,
                    track_id=resource_id,
                    source_message=status_message,
                    track=None,
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

                    "❌ *Could not download this "
                    "Spotify track.*\n\n"

                    "The Spotify download service may "
                    "be temporarily unavailable.",
                )

                return

        # ----------------------------------------------------
        # ALBUM / PLAYLIST / ARTIST
        # ----------------------------------------------------

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

            "The current bot supports direct "
            "Spotify *track* downloads.\n\n"

            "For an individual song, send its "
            "Spotify track URL or use:\n"
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
            query,
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

                "Try adding the artist name.\n\n"

                "Example:\n"
                "`Apna Bana Le Arijit Singh spotify`",
            )

            return

        keyboard = []

        context.user_data[
            "spotify_results"
        ] = results

        for index, track in enumerate(
            results[:SPOTIFY_RESULT_LIMIT]
        ):

            title = (
                track.get("title")
                or "Unknown Track"
            )

            artist = (
                track.get("artist")
                or "Unknown Artist"
            )

            button_text = (
                f"{index + 1}. "
                f"{str(title)[:35]}"
                f" — "
                f"{str(artist)[:25]}"
            )

            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text,
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

            "🎵 *Spotify results*\n\n"
            f"Search: `{query[:100]}`\n\n"
            "Select the track you want:",
        )

        await status_message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
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

            "The Spotify search service may "
            "be temporarily unavailable.\n\n"

            "Please try again.",
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

    data = query.data or ""

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    if data == "spotify_cancel":

        context.user_data.pop(
            "spotify_results",
            None,
        )

        try:

            await query.edit_message_text(
                "❌ Spotify selection cancelled."
            )

        except Exception:

            pass

        return

    # --------------------------------------------------------
    # SELECT
    # --------------------------------------------------------

    match = re.match(
        r"^spotify_select:(\d+)$",
        data,
    )

    if not match:
        return

    index = int(
        match.group(1)
    )

    results = context.user_data.get(
        "spotify_results",
        [],
    )

    if (
        index < 0
        or index >= len(results)
    ):

        await query.edit_message_text(
            "❌ This result is no longer available."
        )

        return

    track = results[index]

    title = (
        track.get("title")
        or "Unknown Track"
    )

    artist = (
        track.get("artist")
        or "Unknown Artist"
    )

    # --------------------------------------------------------
    # GET SPOTIFY TRACK ID
    # --------------------------------------------------------

    track_id = (
        track.get("id")
        or track.get("track_id")
        or track.get("spotify_id")
    )

    if not track_id:

        await safe_edit(

            query.message,

            "❌ *Spotify track ID is missing.*\n\n"

            "This search result cannot be "
            "downloaded.",
        )

        context.user_data.pop(
            "spotify_results",
            None,
        )

        return

    try:

        await query.edit_message_text(

            "🎵 *Spotify track selected*\n\n"

            f"🎧 {str(title)[:100]}\n"
            f"👤 {str(artist)[:100]}\n\n"

            "🔎 *Fetching song from Spotify...*",

            parse_mode="Markdown",
        )

        await process_spotify_download(

            update=update,

            context=context,

            track_id=str(track_id),

            source_message=query.message,

            track=track,
        )

    except Exception as error:

        print(
            "Spotify selection error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(

            query.message,

            "❌ *Could not download this "
            "Spotify track.*\n\n"

            "Please try another result.",
        )

    finally:

        context.user_data.pop(
            "spotify_results",
            None,
        )


# ============================================================
# SPOTIFY AUDIO DOWNLOAD PROCESSOR
# ============================================================

async def process_spotify_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    track_id: str,
    source_message,
    track=None,
):

    status_message = source_message

    audio_path = None
    job_dir = None

    try:

        if not track_id:

            raise DownloadError(
                "Spotify track ID is missing."
            )

        if DOWNLOAD_SEMAPHORE.locked():

            await safe_edit(

                status_message,

                "⏳ *You're in the queue...*\n\n"

                "Another download is currently "
                "being processed.\n\n"

                "I'll start yours automatically.",
            )

        async with DOWNLOAD_SEMAPHORE:

            title = (
                track.get("title")
                if track
                else "Spotify track"
            )

            artist = (
                track.get("artist")
                if track
                else ""
            )

            await safe_edit(

                status_message,

                "⬇️ *Downloading from Spotify...*\n\n"

                f"🎵 {str(title or 'Spotify track')[:100]}\n"
                f"👤 {str(artist or '')[:100]}\n\n"

                "⚙️ Fetching audio...",
            )

            result = await asyncio.to_thread(

                download_spotify_song,

                track_id,

                DOWNLOAD_DIR,
            )

        if not result:

            raise DownloadError(
                "Spotify downloader returned no result."
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
                "Spotify audio file was not found."
            )

        if audio_path.stat().st_size == 0:

            raise DownloadError(
                "Spotify audio file is empty."
            )

        title = (
            result.get("title")
            or (
                track.get("title")
                if track
                else None
            )
            or "Audio"
        )

        artist = (
            result.get("artist")
            or (
                track.get("artist")
                if track
                else None
            )
            or ""
        )

        duration = result.get(
            "duration"
        )

        filename = (
            result.get("filename")
            or audio_path.name
        )

        quality = (
            result.get("quality")
            or result.get("quality_text")
            or "Spotify Audio"
        )

        file_size = (
            audio_path.stat().st_size
        )

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

        formatted_duration = (
            format_duration(
                duration
            )
        )

        if formatted_duration:

            caption_parts.append(
                f"⏱️ {formatted_duration}"
            )

        caption_parts.append(
            f"🎧 {str(quality)[:150]}"
        )

        caption_parts.append(
            "🔎 Source: Spotify"
        )

        caption_parts.append(
            f"\n🤖 Audio Bot v{BOT_VERSION}"
        )

        caption = "\n".join(
            caption_parts
        )

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

            "✅ *Spotify download complete!*\n\n"

            f"🎵 {str(title)[:100]}\n"
            f"📦 {format_bytes(file_size)}\n"
            f"🎧 {str(quality)[:100]}",
        )

        await asyncio.sleep(2)

        await delete_message(
            status_message
        )

    except DownloadError as error:

        print(
            "Spotify DownloadError:",
            str(error),
        )

        await safe_edit(

            status_message,

            "❌ *Spotify download failed.*\n\n"

            f"{str(error)[:500]}",
        )

    except asyncio.TimeoutError:

        print(
            "Spotify download timed out."
        )

        await safe_edit(

            status_message,

            "⏱️ *Spotify download timed out.*\n\n"

            "Please try again.",
        )

    except Exception as error:

        print(
            "Unexpected Spotify processing error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(

            status_message,

            "❌ *Spotify download failed.*\n\n"

            "The Spotify download service may "
            "be temporarily unavailable.",
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
                    "Spotify cleanup error:",
                    error,
                )


# ============================================================
# YOUTUBE AUDIO DOWNLOAD PROCESSOR
# ============================================================

async def process_youtube_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    original_url: str,
    source_message,
):

    status_message = source_message

    audio_path = None
    job_dir = None

    try:

        if DOWNLOAD_SEMAPHORE.locked():

            await safe_edit(

                status_message,

                "⏳ *You're in the queue...*\n\n"

                "Another download is currently "
                "being processed.\n\n"

                "I'll start yours automatically.",
            )

        async with DOWNLOAD_SEMAPHORE:

            await safe_edit(

                status_message,

                "⬇️ *Downloading from YouTube...*\n\n"

                "⚙️ Processing with "
                "yt-dlp + FFmpeg...",
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
                "Downloaded audio file "
                "was not found."
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

        duration = result.get(
            "duration"
        )

        filename = (
            result.get("filename")
            or audio_path.name
        )

        quality = (
            result.get("quality")
            or result.get("quality_text")
            or "MP3 • 192 kbps"
        )

        file_size = (
            audio_path.stat().st_size
        )

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

        formatted_duration = (
            format_duration(
                duration
            )
        )

        if formatted_duration:

            caption_parts.append(
                f"⏱️ {formatted_duration}"
            )

        caption_parts.append(
            f"🎧 {str(quality)[:150]}"
        )

        caption_parts.append(
            "🔎 Source: YouTube"
        )

        caption_parts.append(
            f"\n🤖 Audio Bot v{BOT_VERSION}"
        )

        caption = "\n".join(
            caption_parts
        )

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

            "✅ *YouTube download complete!*\n\n"

            f"🎵 {str(title)[:100]}\n"
            f"📦 {format_bytes(file_size)}\n"
            f"🎧 {str(quality)[:100]}",
        )

        await asyncio.sleep(2)

        await delete_message(
            status_message
        )

    except DownloadError as error:

        error_text = str(error)

        print(
            "YouTube DownloadError:",
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
                "This video requires access that "
                "the bot cannot provide."
            )

        elif (
            "rate-limit" in lowered
            or "rate limit" in lowered
            or "429" in lowered
        ):

            message = (
                "⏳ *YouTube is temporarily busy*\n\n"
                "YouTube is rate-limiting this "
                "server right now.\n\n"
                "Please try again after a short while."
            )

        elif (
            "sign in" in lowered
            or "bot" in lowered
        ):

            message = (
                "🛡️ *YouTube verification required*\n\n"
                "YouTube is currently requiring "
                "additional verification for this request.\n\n"
                "Please try again later."
            )

        elif "403" in lowered:

            message = (
                "⚠️ *YouTube temporarily rejected "
                "the request.*\n\n"
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
                "The maximum allowed duration "
                "has been exceeded."
            )

        else:

            message = (
                "❌ *YouTube download failed.*\n\n"
                "The video may be unavailable, "
                "restricted, unsupported, or "
                "temporarily blocked.\n\n"
                "Please try another link."
            )

        await safe_edit(
            status_message,
            message,
        )

    except asyncio.TimeoutError:

        print(
            "YouTube download timed out."
        )

        await safe_edit(

            status_message,

            "⏱️ *YouTube download timed out.*\n\n"

            "Please try again.",
        )

    except Exception as error:

        print(
            "Unexpected YouTube processing error:",
            type(error).__name__,
            str(error),
        )

        await safe_edit(

            status_message,

            "❌ *Something went wrong.*\n\n"

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
                    "YouTube cleanup error:",
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

    status_message = (
        await update.message.reply_text(

            "🔍 *Analyzing YouTube link...*\n\n"
            "Please wait...",

            parse_mode="Markdown",
        )
    )

    await process_youtube_download(

        update=update,

        context=context,

        original_url=original_url,

        source_message=status_message,
    )


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
        update.message.text or ""
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

        await process_youtube_download(

            update=update,

            context=context,

            original_url=youtube_url,

            source_message=status_message,
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

    # --------------------------------------------------------
    # YOUTUBE URL
    # --------------------------------------------------------

    if is_youtube_url(text):

        await handle_youtube_link(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # SPOTIFY REQUEST
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
    # DEFAULT = YOUTUBE SEARCH
    # --------------------------------------------------------

    await handle_youtube_search(
        update,
        context,
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
            "BOT_TOKEN environment variable "
            "is missing."
        )

    if not RENDER_EXTERNAL_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL environment "
            "variable is missing."
        )

    if not WEBHOOK_PATH:

        raise RuntimeError(
            "WEBHOOK_PATH cannot be empty."
        )

    cleanup_download_directory()

    # --------------------------------------------------------
    # TELEGRAM APPLICATION
    # --------------------------------------------------------

    application = (
        ApplicationBuilder()

        .token(BOT_TOKEN)

        .concurrent_updates(True)

        .post_init(post_init)

        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
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
    # SPOTIFY CALLBACKS
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
    # GREETINGS
    # --------------------------------------------------------

    application.add_handler(

        MessageHandler(

            filters.Regex(

                re.compile(

                    r"^\s*"

                    r"(hey|heyy|heyyy|"
                    r"hello|helloo|hellooo|"
                    r"helo|heloo|"
                    r"hi|hii|hiii|hiiii|"
                    r"hy|hyy|hyyy|"
                    r"hye|hyee|"
                    r"yo|sup|namaste)"

                    r"\s*[!.]?\s*$",

                    re.IGNORECASE,
                )
            ),

            greeting,
        )
    )

    # --------------------------------------------------------
    # NORMAL TEXT
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
    # STARTUP LOG
    # --------------------------------------------------------

    print(
        "============================================"
    )

    print(
        f"🤖 Audio Bot v{BOT_VERSION}"
    )

    print(
        "🎧 YouTube Engine: yt-dlp + FFmpeg"
    )

    print(
        "🎵 Spotify Engine: RapidAPI Downloader"
    )

    print(
        "🔐 YouTube PO Tokens: BGUTIL"
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
        "🌐 Telegram mode: WEBHOOK"
    )

    print(
        f"🌐 Webhook URL: {WEBHOOK_URL}"
    )

    print(
        f"🌐 Webhook path: /{WEBHOOK_PATH}"
    )

    print(
        f"🌐 Public port: {PORT}"
    )

    print(
        "============================================"
    )

    # --------------------------------------------------------
    # TELEGRAM WEBHOOK
    # --------------------------------------------------------

    application.run_webhook(

        listen="0.0.0.0",

        port=PORT,

        url_path=WEBHOOK_PATH,

        webhook_url=WEBHOOK_URL,

        secret_token=(
            WEBHOOK_SECRET
            if WEBHOOK_SECRET
            else None
        ),

        # Keep pending Telegram updates when
        # Render wakes from its Free-tier sleep.
        drop_pending_updates=False,

        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()