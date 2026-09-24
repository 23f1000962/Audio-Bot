import os
import re
import asyncio
import shutil
from pathlib import Path
from typing import Optional

import yt_dlp


# ============================================================
# CONFIGURATION
# ============================================================

MAX_FILE_SIZE_MB = int(
    os.getenv("MAX_FILE_SIZE_MB", "49")
)

MAX_FILE_SIZE_BYTES = (
    MAX_FILE_SIZE_MB * 1024 * 1024
)

MAX_DURATION = int(
    os.getenv("MAX_DURATION", "7200")
)

DOWNLOAD_TIMEOUT = int(
    os.getenv("DOWNLOAD_TIMEOUT", "600")
)

AUDIO_OUTPUT = os.getenv(
    "AUDIO_OUTPUT",
    "mp3"
).lower()

AUDIO_QUALITY = os.getenv(
    "AUDIO_QUALITY",
    "320"
)

DOWNLOAD_RETRIES = int(
    os.getenv("DOWNLOAD_RETRIES", "3")
)


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================

class DownloadError(Exception):
    """General downloader error."""
    pass


class FileTooLargeError(DownloadError):
    """Downloaded file exceeds configured file-size limit."""
    pass


class DurationTooLongError(DownloadError):
    """Video duration exceeds configured limit."""
    pass


class VideoUnavailableError(DownloadError):
    """Video is unavailable/private/restricted."""
    pass


# ============================================================
# HELPERS
# ============================================================

def sanitize_filename(
    filename: str,
    max_length: int = 120,
) -> str:

    filename = str(
        filename or "audio"
    )

    filename = re.sub(
        r"[\x00-\x1f\x7f]",
        "",
        filename,
    )

    filename = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        filename,
    )

    filename = re.sub(
        r"\s+",
        " ",
        filename,
    )

    filename = filename.strip(
        " ."
    )

    if not filename:
        filename = "audio"

    return filename[:max_length]


def ensure_ffmpeg():

    ffmpeg_path = shutil.which(
        "ffmpeg"
    )

    if not ffmpeg_path:
        raise DownloadError(
            "FFmpeg is not installed on the server."
        )

    return ffmpeg_path


def format_bytes(
    size: Optional[int],
) -> str:

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


# ============================================================
# PROGRESS TRACKER
# ============================================================

class ProgressTracker:

    def __init__(
        self,
        callback=None,
    ):

        self.callback = callback
        self.last_percent = -1

    def hook(
        self,
        data,
    ):

        status = data.get(
            "status"
        )

        if status == "downloading":

            downloaded = data.get(
                "downloaded_bytes",
                0,
            )

            total = (
                data.get(
                    "total_bytes"
                )
                or data.get(
                    "total_bytes_estimate"
                )
            )

            percent = None

            if total:

                percent = (
                    downloaded / total
                ) * 100

                percent = max(
                    0,
                    min(
                        100,
                        percent,
                    ),
                )

            if (
                percent is not None
                and int(percent)
                == self.last_percent
            ):
                return

            if percent is not None:

                self.last_percent = int(
                    percent
                )

            progress = {
                "status": "Downloading",
                "percent": percent,
                "downloaded": downloaded,
                "total": total,
                "speed": data.get(
                    "speed"
                ),
                "eta": data.get(
                    "eta"
                ),
            }

            self._send(
                progress
            )

        elif status == "finished":

            self._send(
                {
                    "status": "Processing audio",
                    "percent": 100,
                }
            )

    def _send(
        self,
        progress,
    ):

        if not self.callback:
            return

        try:

            result = self.callback(
                progress
            )

            if asyncio.iscoroutine(
                result
            ):

                try:

                    loop = (
                        asyncio.get_running_loop()
                    )

                    loop.create_task(
                        result
                    )

                except RuntimeError:
                    pass

        except Exception as error:

            print(
                "Progress callback error:",
                error,
            )


# ============================================================
# YT-DLP LOGGER
# ============================================================

class YTDLPLogger:

    def debug(
        self,
        message,
    ):

        if message.startswith(
            "[debug]"
        ):
            return

        print(
            "yt-dlp:",
            message,
        )

    def warning(
        self,
        message,
    ):

        print(
            "yt-dlp warning:",
            message,
        )

    def error(
        self,
        message,
    ):

        print(
            "yt-dlp error:",
            message,
        )


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_dir: Path,
    progress_tracker: ProgressTracker,
):

    output_template = str(
        output_dir
        / "%(id)s.%(ext)s"
    )

    options = {

        # ----------------------------------------------------
        # BEST AVAILABLE AUDIO
        # ----------------------------------------------------

        "format": (
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/"
            "best"
        ),

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "ignoreerrors": False,

        # ----------------------------------------------------
        # RETRIES
        # ----------------------------------------------------

        "retries": DOWNLOAD_RETRIES,

        "fragment_retries": DOWNLOAD_RETRIES,

        "file_access_retries": DOWNLOAD_RETRIES,

        "extractor_retries": DOWNLOAD_RETRIES,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": True,

        # ----------------------------------------------------
        # PROGRESS
        # ----------------------------------------------------

        "progress_hooks": [
            progress_tracker.hook
        ],

        # ----------------------------------------------------
        # FILENAMES
        # ----------------------------------------------------

        "restrictfilenames": False,

        "windowsfilenames": True,

        # ----------------------------------------------------
        # THUMBNAIL + METADATA
        # ----------------------------------------------------

        "writethumbnail": True,

        "writeinfojson": False,

        "addmetadata": True,

        # ----------------------------------------------------
        # POST PROCESSING
        # ----------------------------------------------------

        "postprocessors": [

            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": AUDIO_OUTPUT,
                "preferredquality": AUDIO_QUALITY,
            },

            {
                "key": "FFmpegMetadata",
            },

            {
                "key": "EmbedThumbnail",
            },

        ],

        # ----------------------------------------------------
        # HTTP HEADERS
        # ----------------------------------------------------

        "http_headers": {

            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),

        },

        # ----------------------------------------------------
        # LOGGER
        # ----------------------------------------------------

        "logger": YTDLPLogger(),

    }

    return options


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def classify_download_error(
    error,
):
    """
    Convert yt-dlp errors into cleaner bot errors.
    """

    message = str(
        error
    )

    lowered = message.lower()

    restricted_keywords = [
        "private video",
        "video unavailable",
        "sign in",
        "members-only",
        "age-restricted",
        "this video is not available",
        "confirm your age",
    ]

    if any(
        keyword in lowered
        for keyword in restricted_keywords
    ):

        return VideoUnavailableError(
            "This YouTube video is unavailable, "
            "private, restricted, age-restricted, "
            "or requires authentication."
        )

    return DownloadError(
        message[:700]
    )


# ============================================================
# EXTRACT VIDEO INFORMATION
# ============================================================

def extract_info(
    url: str,
    options: dict,
):

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=False,
            )

        if not info:

            raise VideoUnavailableError(
                "YouTube did not return video information."
            )

        return info

    except yt_dlp.utils.DownloadError as error:

        raise classify_download_error(
            error
        ) from error


# ============================================================
# FIND GENERATED FILES
# ============================================================

def find_audio_file(
    output_dir: Path,
    video_id: str,
):

    candidates = []

    # --------------------------------------------------------
    # First try exact video ID
    # --------------------------------------------------------

    for file in output_dir.iterdir():

        if not file.is_file():
            continue

        if file.name.startswith(
            f"{video_id}."
        ):

            if file.suffix.lower() in {
                ".mp3",
                ".m4a",
                ".aac",
                ".opus",
                ".ogg",
                ".wav",
                ".flac",
                ".webm",
            }:

                candidates.append(
                    file
                )

    # --------------------------------------------------------
    # Fallback: search all supported audio files
    # --------------------------------------------------------

    if not candidates:

        candidates = [

            file

            for file in output_dir.iterdir()

            if (
                file.is_file()
                and file.suffix.lower()
                in {
                    ".mp3",
                    ".m4a",
                    ".aac",
                    ".opus",
                    ".ogg",
                    ".wav",
                    ".flac",
                    ".webm",
                }
            )

        ]

    if not candidates:

        return None

    # Newest generated file
    return max(
        candidates,
        key=lambda file:
        file.stat().st_mtime,
    )


# ============================================================
# CLEAN TEMPORARY FILES
# ============================================================

def cleanup_related_files(
    output_dir: Path,
    video_id: Optional[str] = None,
    keep: Optional[Path] = None,
):

    if not output_dir.exists():
        return

    temporary_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".part",
        ".ytdl",
        ".temp",
        ".json",
    }

    for file in output_dir.iterdir():

        if not file.is_file():
            continue

        # Never delete the final audio here
        if keep and file.resolve() == keep.resolve():
            continue

        should_delete = False

        if video_id:

            if file.name.startswith(
                f"{video_id}."
            ):

                should_delete = True

        if file.suffix.lower() in temporary_extensions:

            should_delete = True

        if should_delete:

            try:

                file.unlink()

            except Exception as error:

                print(
                    "Temporary cleanup error:",
                    error,
                )


# ============================================================
# DOWNLOAD SYNCHRONOUSLY
# ============================================================

def download_sync(
    url: str,
    output_dir: Path,
    progress_callback=None,
):

    ensure_ffmpeg()

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    progress_tracker = ProgressTracker(
        progress_callback
    )

    options = build_ydl_options(
        output_dir,
        progress_tracker,
    )

    print(
        "Extracting:",
        url,
    )

    # ========================================================
    # EXTRACT METADATA
    # ========================================================

    info = extract_info(
        url,
        options,
    )

    duration = info.get(
        "duration"
    )

    # ========================================================
    # DURATION CHECK
    # ========================================================

    if (
        duration
        and duration > MAX_DURATION
    ):

        hours = MAX_DURATION // 3600

        minutes = (
            MAX_DURATION % 3600
        ) // 60

        raise DurationTooLongError(
            "This audio is too long.\n\n"
            f"Maximum allowed duration: "
            f"{hours}h {minutes}m."
        )

    # ========================================================
    # METADATA
    # ========================================================

    title = (
        info.get("track")
        or info.get("title")
        or "Audio"
    )

    artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or ""
    )

    album = (
        info.get("album")
        or ""
    )

    album_artist = (
        info.get("album_artist")
        or artist
    )

    uploader = (
        info.get("uploader")
        or ""
    )

    thumbnail = (
        info.get("thumbnail")
        or ""
    )

    webpage_url = (
        info.get("webpage_url")
        or url
    )

    video_id = (
        info.get("id")
        or "audio"
    )

    print(
        "Title:",
        title,
    )

    print(
        "Artist:",
        artist,
    )

    print(
        "Duration:",
        duration,
    )

    print(
        "Video ID:",
        video_id,
    )

    # ========================================================
    # ESTIMATE SOURCE SIZE
    # ========================================================

    estimated_size = (
        info.get("filesize")
        or info.get("filesize_approx")
    )

    requested_formats = (
        info.get("requested_formats")
        or []
    )

    if not estimated_size:

        for fmt in requested_formats:

            size = (
                fmt.get("filesize")
                or fmt.get("filesize_approx")
            )

            if size:

                estimated_size = (
                    estimated_size or 0
                ) + size

    # Conservative pre-download check.
    if (
        estimated_size
        and estimated_size
        > MAX_FILE_SIZE_BYTES * 2
    ):

        raise FileTooLargeError(
            "The source audio is too large "
            "to safely process for Telegram."
        )

    # ========================================================
    # DOWNLOAD
    # ========================================================

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.download(
                [url]
            )

    except yt_dlp.utils.DownloadError as error:

        raise classify_download_error(
            error
        ) from error

    # ========================================================
    # FIND FINAL AUDIO
    # ========================================================

    audio_path = find_audio_file(
        output_dir,
        video_id,
    )

    if not audio_path:

        raise DownloadError(
            "yt-dlp completed but no audio "
            "file was produced."
        )

    # ========================================================
    # FINAL FILE SIZE
    # ========================================================

    file_size = (
        audio_path.stat().st_size
    )

    print(
        "Final file:",
        audio_path,
    )

    print(
        "Final size:",
        format_bytes(
            file_size
        ),
    )

    # ========================================================
    # FILE SIZE LIMIT
    # ========================================================

    if (
        file_size
        > MAX_FILE_SIZE_BYTES
    ):

        try:

            audio_path.unlink()

        except Exception:
            pass

        cleanup_related_files(
            output_dir,
            video_id,
        )

        raise FileTooLargeError(
            "The final audio file is "
            f"{format_bytes(file_size)}, "
            "which exceeds the configured "
            f"upload safety limit of "
            f"{MAX_FILE_SIZE_MB} MB."
        )

    # ========================================================
    # CLEAN / RENAME FINAL FILE
    # ========================================================

    clean_title = sanitize_filename(
        title,
        90,
    )

    extension = (
        audio_path.suffix.lower()
    )

    final_name = (
        f"{clean_title}{extension}"
    )

    final_path = (
        output_dir / final_name
    )

    counter = 1

    while (
        final_path.exists()
        and final_path != audio_path
    ):

        final_path = (
            output_dir
            / f"{clean_title} "
            f"({counter}){extension}"
        )

        counter += 1

    if final_path != audio_path:

        try:

            audio_path.rename(
                final_path
            )

            audio_path = final_path

        except Exception as error:

            print(
                "Rename failed:",
                error,
            )

    # ========================================================
    # FIND THUMBNAIL
    # ========================================================

    thumbnail_path = None

    thumbnail_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }

    possible_thumbnails = [

        file

        for file in output_dir.iterdir()

        if (
            file.is_file()
            and file.suffix.lower()
            in thumbnail_extensions
        )

    ]

    if possible_thumbnails:

        thumbnail_path = max(
            possible_thumbnails,
            key=lambda file:
            file.stat().st_mtime,
        )

    # ========================================================
    # RETURN RESULT
    # ========================================================

    return {
        "path": str(
            audio_path
        ),

        "filename": (
            audio_path.name
        ),

        "title": str(
            title
        ),

        "artist": str(
            artist
        ),

        "album": str(
            album
        ),

        "album_artist": str(
            album_artist
        ),

        "uploader": str(
            uploader
        ),

        "duration": duration,

        "thumbnail": (
            str(thumbnail_path)
            if thumbnail_path
            else thumbnail
        ),

        "webpage_url": (
            webpage_url
        ),

        "video_id": (
            video_id
        ),

        "filesize": (
            file_size
        ),

        "format": (
            extension.lstrip(".")
        ),

        "quality": (
            AUDIO_QUALITY
        ),
    }


# ============================================================
# ASYNC DOWNLOAD WRAPPER
# ============================================================

async def download_audio(
    url: str,
    output_dir: Path,
    progress_callback=None,
):

    loop = asyncio.get_running_loop()

    try:

        return await asyncio.wait_for(

            loop.run_in_executor(

                None,

                lambda:
                download_sync(
                    url,
                    output_dir,
                    progress_callback,
                ),

            ),

            timeout=DOWNLOAD_TIMEOUT,

        )

    except asyncio.TimeoutError:

        raise DownloadError(
            "The download took too long "
            "and was cancelled."
        )