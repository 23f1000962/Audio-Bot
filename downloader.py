import os
import re
import asyncio
import shutil
from pathlib import Path
from typing import Optional, Callable, Awaitable

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
    """
    General downloader error.
    """
    pass


class FileTooLargeError(DownloadError):
    """
    Downloaded file exceeds Telegram/file-size limit.
    """
    pass


class DurationTooLongError(DownloadError):
    """
    Video duration exceeds configured limit.
    """
    pass


class VideoUnavailableError(DownloadError):
    """
    Video is unavailable/private/restricted.
    """
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

    return filename[
        :max_length
    ]


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
# PROGRESS HOOK
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
                    downloaded
                    / total
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
# URL OPTIONS
# ============================================================

def build_ydl_options(
    output_dir: Path,
    progress_tracker: ProgressTracker,
):

    output_template = str(
        output_dir
        / "%(id)s.%(ext)s"
    )

    # --------------------------------------------------------
    # Best available audio
    # --------------------------------------------------------
    #
    # Prefer audio-only formats.
    #
    # `bestaudio/best` allows yt-dlp to fall back when a
    # separate audio stream is unavailable.
    #
    # --------------------------------------------------------

    options = {

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

        "retries": DOWNLOAD_RETRIES,

        "fragment_retries": DOWNLOAD_RETRIES,

        "file_access_retries": DOWNLOAD_RETRIES,

        "extractor_retries": DOWNLOAD_RETRIES,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": True,

        "progress_hooks": [
            progress_tracker.hook
        ],

        "restrictfilenames": False,

        "windowsfilenames": True,

        "writethumbnail": True,

        "writeinfojson": False,

        "noplaylist": True,

        "extract_flat": False,

        # Metadata
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

        # Better metadata
        "addmetadata": True,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
        },

        "logger": YTDLPLogger(),

    }

    return options


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

        message = str(
            error
        )

        lowered = message.lower()

        if any(
            word in lowered
            for word in [
                "private video",
                "video unavailable",
                "sign in",
                "members-only",
                "age-restricted",
            ]
        ):

            raise VideoUnavailableError(
                "This YouTube video is unavailable, "
                "private, restricted, or requires "
                "authentication."
            ) from error

        raise DownloadError(
            f"YouTube extraction failed: {message[:500]}"
        ) from error


# ============================================================
# DOWNLOAD
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

    progress_tracker = (
        ProgressTracker(
            progress_callback
        )
    )

    options = build_ydl_options(
        output_dir,
        progress_tracker,
    )

    print(
        "Extracting:",
        url,
    )

    # --------------------------------------------------------
    # First obtain metadata
    # --------------------------------------------------------

    info = extract_info(
        url,
        options,
    )

    duration = info.get(
        "duration"
    )

    if (
        duration
        and duration > MAX_DURATION
    ):

        raise DurationTooLongError(
            "This audio is too long.\n\n"
            f"Maximum allowed duration: "
            f"{MAX_DURATION // 3600}h "
            f"{(MAX_DURATION % 3600) // 60}m."
        )

    title = (
        info.get(
            "track"
        )
        or info.get(
            "title"
        )
        or "Audio"
    )

    artist = (
        info.get(
            "artist"
        )
        or info.get(
            "creator"
        )
        or info.get(
            "uploader"
        )
        or ""
    )

    album = (
        info.get(
            "album"
        )
        or ""
    )

    album_artist = (
        info.get(
            "album_artist"
        )
        or artist
    )

    uploader = (
        info.get(
            "uploader"
        )
        or ""
    )

    thumbnail = (
        info.get(
            "thumbnail"
        )
        or ""
    )

    webpage_url = (
        info.get(
            "webpage_url"
        )
        or url
    )

    video_id = (
        info.get(
            "id"
        )
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

    # --------------------------------------------------------
    # Estimate filesize before downloading
    # --------------------------------------------------------

    requested_formats = (
        info.get(
            "requested_formats"
        )
        or []
    )

    estimated_size = (
        info.get(
            "filesize"
        )
        or info.get(
            "filesize_approx"
        )
    )

    if not estimated_size:

        for fmt in requested_formats:

            size = (
                fmt.get(
                    "filesize"
                )
                or fmt.get(
                    "filesize_approx"
                )
            )

            if size:

                estimated_size = (
                    estimated_size or 0
                ) + size

    if (
        estimated_size
        and estimated_size
        > MAX_FILE_SIZE_BYTES * 2
    ):

        raise FileTooLargeError(
            "The source audio is too large "
            "to safely process for Telegram."
        )

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.download(
                [url]
            )

    except yt_dlp.utils.DownloadError as error:

        message = str(
            error
        )

        lowered = message.lower()

        if (
            "private video" in lowered
            or "video unavailable" in lowered
            or "sign in" in lowered
            or "members-only" in lowered
        ):

            raise VideoUnavailableError(
                "This video is unavailable or restricted."
            ) from error

        raise DownloadError(
            f"Audio download failed: {message[:600]}"
        ) from error

    # --------------------------------------------------------
    # Find resulting audio file
    # --------------------------------------------------------

    candidates = []

    for file in output_dir.iterdir():

        if not file.is_file():
            continue

        if file.name.startswith(
            f"{video_id}."
        ):

            candidates.append(
                file
            )

    # yt-dlp may use a different sanitized name.
    if not candidates:

        candidates = [
            file
            for file in output_dir.iterdir()
            if file.is_file()
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
        ]

    if not candidates:

        raise DownloadError(
            "yt-dlp completed but no audio file "
            "was produced."
        )

    # Choose newest file.
    audio_path = max(
        candidates,
        key=lambda file:
        file.stat().st_mtime,
    )

    # --------------------------------------------------------
    # File size check
    # --------------------------------------------------------

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