import os
import re
import asyncio
import inspect
import shutil
import subprocess
import tempfile
import time
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

# Target bitrate.
#
# Examples:
# 128
# 192
# 256
# 320
#
# Current recommended value:
# 192
AUDIO_QUALITY = os.getenv(
    "AUDIO_QUALITY",
    "192"
)

DOWNLOAD_RETRIES = int(
    os.getenv("DOWNLOAD_RETRIES", "3")
)

MAX_CONCURRENT_DOWNLOADS = max(
    1,
    int(
        os.getenv(
            "MAX_CONCURRENT_DOWNLOADS",
            "1"
        )
    )
)

# Delay before trying another YouTube client.
# Kept low for faster fallback.
FALLBACK_DELAY = max(
    0,
    int(
        os.getenv(
            "FALLBACK_DELAY",
            "1"
        )
    )
)


# ============================================================
# YOUTUBE COOKIES
# ============================================================

# Render Secret File:
#
# /etc/secrets/cookies.txt
#
# Render Secret Files are READ-ONLY.
#
# Therefore we copy the secret into /tmp and let yt-dlp
# use the writable runtime copy.
#
# NEVER commit cookies.txt to GitHub.
# NEVER print cookie contents.

COOKIE_FILE = Path(
    os.getenv(
        "YOUTUBE_COOKIE_FILE",
        "/etc/secrets/cookies.txt"
    )
)

RUNTIME_COOKIE_FILE = Path(
    os.getenv(
        "YOUTUBE_RUNTIME_COOKIE_FILE",
        "/tmp/youtube-cookies.txt"
    )
)


def cookies_available() -> bool:

    try:

        return (
            COOKIE_FILE.exists()
            and COOKIE_FILE.is_file()
            and COOKIE_FILE.stat().st_size > 0
        )

    except Exception:

        return False


def prepare_cookie_file() -> bool:

    if not cookies_available():

        return False

    try:

        RUNTIME_COOKIE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copyfile(
            COOKIE_FILE,
            RUNTIME_COOKIE_FILE,
        )

        try:

            os.chmod(
                RUNTIME_COOKIE_FILE,
                0o600,
            )

        except OSError:

            pass

        print(
            "YouTube cookies: available "
            "(copied to writable runtime file)"
        )

        return True

    except Exception as error:

        print(
            "YouTube cookies: failed to prepare "
            f"runtime copy: {type(error).__name__}"
        )

        return False


def verify_cookie_file():

    if cookies_available():

        print(
            "YouTube cookies: available"
        )

    else:

        print(
            "YouTube cookies: NOT FOUND"
        )

        print(
            f"Expected cookie file: {COOKIE_FILE}"
        )


# ============================================================
# INTERNAL CONCURRENCY
# ============================================================

_download_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)


# ============================================================
# EXCEPTIONS
# ============================================================

class DownloadError(Exception):
    pass


class FileTooLargeError(DownloadError):
    pass


class DurationTooLongError(DownloadError):
    pass


class VideoUnavailableError(DownloadError):
    pass


# ============================================================
# HELPERS
# ============================================================

def sanitize_filename(
    filename: str,
    max_length: int = 180,
) -> str:

    """
    Convert the YouTube title into a filesystem-safe filename.

    Example:

        Tera Ban Jaunga
        ->
        Tera Ban Jaunga

    Invalid filesystem characters are replaced with "_".
    """

    filename = str(
        filename or "audio"
    )

    # Remove control characters.
    filename = re.sub(
        r"[\x00-\x1f\x7f]",
        "",
        filename,
    )

    # Characters that are invalid on Windows/Linux filenames.
    filename = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        filename,
    )

    # Collapse repeated whitespace.
    filename = re.sub(
        r"\s+",
        " ",
        filename,
    )

    # Remove trailing spaces/dots.
    filename = filename.strip(
        " ."
    )

    if not filename:

        filename = "audio"

    # Windows reserved filenames.
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }

    if filename.upper() in reserved:

        filename = f"_{filename}"

    return filename[:max_length]


def ensure_ffmpeg():

    if not shutil.which(
        "ffmpeg"
    ):

        raise DownloadError(
            "FFmpeg is not installed on "
            "the server."
        )

    if not shutil.which(
        "ffprobe"
    ):

        raise DownloadError(
            "FFprobe is not installed on "
            "the server."
        )


def format_bytes(
    size: Optional[int],
) -> str:

    if size is None:

        return "Unknown"

    size = float(size)

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


def format_duration(
    seconds: Optional[float],
) -> str:

    if not seconds:

        return ""

    try:

        seconds = int(
            float(seconds)
        )

    except (
        TypeError,
        ValueError,
    ):

        return ""

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    remaining = (
        seconds % 60
    )

    if hours:

        return (
            f"{hours}:{minutes:02d}:"
            f"{remaining:02d}"
        )

    return (
        f"{minutes}:{remaining:02d}"
    )


def format_speed(
    speed: Optional[float],
) -> str:

    if not speed:

        return "Unknown"

    return (
        f"{format_bytes(int(speed))}/s"
    )


def format_eta(
    eta: Optional[float],
) -> str:

    if eta is None:

        return "--:--"

    try:

        eta = max(
            0,
            int(eta)
        )

    except (
        TypeError,
        ValueError,
    ):

        return "--:--"

    minutes = eta // 60

    seconds = eta % 60

    if minutes >= 60:

        hours = minutes // 60

        minutes %= 60

        return (
            f"{hours}:{minutes:02d}:"
            f"{seconds:02d}"
        )

    return (
        f"{minutes:02d}:"
        f"{seconds:02d}"
    )


# ============================================================
# QUALITY HELPERS
# ============================================================

def clean_bitrate(
    bitrate: Optional[float],
) -> Optional[int]:

    if bitrate is None:

        return None

    try:

        value = float(
            bitrate
        )

    except (
        TypeError,
        ValueError,
    ):

        return None

    if value <= 0:

        return None

    return int(
        round(value)
    )


def get_source_quality(
    info: dict,
) -> dict:

    formats = (
        info.get("requested_formats")
        or []
    )

    selected = (
        formats[0]
        if formats
        else info
    )

    source_bitrate = (
        clean_bitrate(
            selected.get("abr")
        )
        or clean_bitrate(
            selected.get("tbr")
        )
        or clean_bitrate(
            info.get("abr")
        )
        or clean_bitrate(
            info.get("tbr")
        )
    )

    source_codec = (
        selected.get("acodec")
        or info.get("acodec")
        or "unknown"
    )

    source_ext = (
        selected.get("ext")
        or info.get("ext")
        or "unknown"
    )

    return {
        "source_bitrate":
            source_bitrate,

        "source_codec":
            source_codec,

        "source_ext":
            source_ext,
    }


def get_audio_quality(
    file_path: Path,
) -> dict:

    result = {

        "output_codec":
            None,

        "output_bitrate":
            None,

        "sample_rate":
            None,

        "channels":
            None,

        "quality_text":
            None,
    }

    try:

        command = [

            "ffprobe",

            "-v",
            "error",

            "-select_streams",
            "a:0",

            "-show_entries",

            "stream="
            "codec_name,"
            "bit_rate,"
            "sample_rate,"
            "channels",

            "-of",
            "default="
            "noprint_wrappers=1",

            str(file_path),
        ]

        process = subprocess.run(

            command,

            capture_output=True,

            text=True,

            timeout=20,

            check=False,
        )

        if process.returncode != 0:

            return result

        values = {}

        for line in (
            process.stdout.splitlines()
        ):

            if "=" not in line:

                continue

            key, value = (
                line.split(
                    "=",
                    1
                )
            )

            values[
                key.strip()
            ] = value.strip()

        codec = values.get(
            "codec_name"
        )

        bitrate = values.get(
            "bit_rate"
        )

        sample_rate = values.get(
            "sample_rate"
        )

        channels = values.get(
            "channels"
        )

        output_bitrate = None

        if bitrate:

            try:

                output_bitrate = int(
                    round(
                        int(bitrate)
                        / 1000
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                pass

        result[
            "output_codec"
        ] = codec

        result[
            "output_bitrate"
        ] = output_bitrate

        result[
            "sample_rate"
        ] = sample_rate

        result[
            "channels"
        ] = channels

        codec_name = (

            codec.upper()

            if codec

            else AUDIO_OUTPUT.upper()
        )

        if output_bitrate:

            result[
                "quality_text"
            ] = (

                f"{codec_name} • "
                f"{output_bitrate} kbps"
            )

        else:

            result[
                "quality_text"
            ] = (

                f"{codec_name} • "
                f"{AUDIO_QUALITY} kbps target"
            )

        return result

    except Exception as error:

        print(
            "FFprobe quality check failed:",
            type(error).__name__,
        )

        return result


# ============================================================
# PROGRESS
# ============================================================

class ProgressTracker:

    def __init__(
        self,
        callback=None,
    ):

        self.callback = callback

        self.last_percent = -1

        self.last_update_time = 0.0

    def _send(
        self,
        payload: dict,
    ):

        if not self.callback:

            return

        try:

            self.callback(
                payload
            )

        except Exception as error:

            print(
                "Progress callback error:",
                type(error).__name__,
            )

    def hook(
        self,
        data,
    ):

        if not self.callback:

            return

        status = data.get(
            "status"
        )

        # ====================================================
        # DOWNLOADING
        # ====================================================

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
                    * 100
                )

                percent = max(
                    0,
                    min(
                        100,
                        percent,
                    ),
                )

            now = time.monotonic()

            # Telegram message updates are throttled.
            #
            # Update approximately every 1.5 seconds
            # OR when progress changes by >= 2%.

            should_update = (

                self.last_percent < 0

                or (

                    percent is not None

                    and (

                        percent
                        - self.last_percent
                        >= 2
                    )
                )

                or (

                    now
                    - self.last_update_time
                    >= 1.5
                )
            )

            if not should_update:

                return

            if percent is not None:

                self.last_percent = (
                    percent
                )

            self.last_update_time = now

            self._send(
                {

                    "status":
                        "Downloading",

                    "percent":
                        percent,

                    "downloaded_bytes":
                        downloaded,

                    "total_bytes":
                        total,

                    "speed":
                        data.get(
                            "speed"
                        ),

                    "eta":
                        data.get(
                            "eta"
                        ),

                    "speed_text":
                        format_speed(
                            data.get(
                                "speed"
                            )
                        ),

                    "eta_text":
                        format_eta(
                            data.get(
                                "eta"
                            )
                        ),
                }
            )

        # ====================================================
        # DOWNLOAD FINISHED
        # ====================================================

        elif status == "finished":

            self._send(
                {

                    "status":
                        "Converting to MP3",

                    "percent":
                        100,
                }
            )


# ============================================================
# LOGGER
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
# DURATION FILTER
# ============================================================

def duration_filter(
    info,
    *,
    incomplete,
):

    duration = info.get(
        "duration"
    )

    if (

        duration

        and duration
        > MAX_DURATION
    ):

        max_duration = (
            format_duration(
                MAX_DURATION
            )
        )

        actual_duration = (
            format_duration(
                duration
            )
        )

        return (

            f"Audio is too long. "
            f"Maximum: {max_duration}. "
            f"Video: {actual_duration}."
        )

    return None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_dir: Path,
    progress_tracker: ProgressTracker,
    player_client: str,
):

    # ========================================================
    # IMPORTANT
    # ========================================================
    #
    # Use YouTube TITLE as the filename.
    #
    # Example:
    #
    # Tera Ban Jaunga.mp3
    #
    # rather than:
    #
    # K0V2m5f2d1A.mp3
    #
    output_template = str(
        output_dir
        / "%(title)s.%(ext)s"
    )

    options = {

        # ====================================================
        # BEST AVAILABLE AUDIO
        # ====================================================

        "format":

            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/best",

        # ====================================================
        # TITLE-BASED OUTPUT
        # ====================================================

        "outtmpl":
            output_template,

        # Do not download playlists.
        "noplaylist":
            True,

        # ====================================================
        # FILENAMES
        # ====================================================

        # Keep Unicode characters, spaces and normal title
        # characters whenever possible.
        "restrictfilenames":
            False,

        # ====================================================
        # DURATION FILTER
        # ====================================================

        "match_filter":
            duration_filter,

        # ====================================================
        # OUTPUT
        # ====================================================

        "quiet":
            True,

        "no_warnings":
            False,

        "ignoreerrors":
            False,

        # ====================================================
        # RETRIES
        # ====================================================

        "retries":
            DOWNLOAD_RETRIES,

        "fragment_retries":
            DOWNLOAD_RETRIES,

        "file_access_retries":
            DOWNLOAD_RETRIES,

        "extractor_retries":
            2,

        "socket_timeout":
            30,

        "continuedl":
            True,

        "overwrites":
            True,

        # ====================================================
        # SPEED
        # ====================================================

        "sleep_interval_requests":
            0.5,

        "sleep_interval":
            0,

        "max_sleep_interval":
            0.5,

        # Download multiple fragments concurrently.
        "concurrent_fragment_downloads":
            4,

        # ====================================================
        # PROGRESS
        # ====================================================

        "progress_hooks": [

            progress_tracker.hook

        ],

        # ====================================================
        # THUMBNAIL / METADATA
        # ====================================================

        "writethumbnail":
            True,

        "addmetadata":
            True,

        # ====================================================
        # POST PROCESSING
        # ====================================================

        "postprocessors": [

            {
                "key":
                    "FFmpegExtractAudio",

                "preferredcodec":
                    AUDIO_OUTPUT,

                "preferredquality":
                    AUDIO_QUALITY,
            },

            {
                "key":
                    "FFmpegMetadata",
            },

            {
                "key":
                    "EmbedThumbnail",
            },
        ],

        # ====================================================
        # YOUTUBE CLIENT
        # ====================================================

        "extractor_args": {

            "youtube": {

                "player_client": [
                    player_client
                ],
            },

            # =================================================
            # BGUTIL PO TOKEN PROVIDER
            # =================================================

            "youtubepot-bgutilhttp": {

                "base_url":
                    "http://127.0.0.1:4416",
            },
        },

        # ====================================================
        # DENO
        # ====================================================

        "js_runtimes": {

            "deno": {

                "path":
                    "/root/.deno/bin/deno",
            },
        },

        # ====================================================
        # EJS
        # ====================================================

        "remote_components": {
            "ejs:npm",
        },

        # ====================================================
        # LANGUAGE
        # ====================================================

        "http_headers": {

            "Accept-Language":
                "en-US,en;q=0.9",
        },

        # ====================================================
        # LOGGER
        # ====================================================

        "logger":
            YTDLPLogger(),
    }

    # ========================================================
    # COOKIES
    # ========================================================

    if RUNTIME_COOKIE_FILE.exists():

        options[
            "cookiefile"
        ] = str(
            RUNTIME_COOKIE_FILE
        )

    return options


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def classify_download_error(
    error,
):

    message = str(
        error
    )

    lowered = (
        message.lower()
    )

    # ========================================================
    # DURATION
    # ========================================================

    if (

        "audio is too long"
        in lowered

        or

        "video is too long"
        in lowered
    ):

        return DurationTooLongError(
            message
        )

    # ========================================================
    # RATE LIMIT
    # ========================================================

    if (

        "http error 429"
        in lowered

        or

        "too many requests"
        in lowered

        or

        "rate limit"
        in lowered
    ):

        return DownloadError(
            "YouTube is temporarily "
            "rate-limiting this server."
        )

    # ========================================================
    # BOT CHECK
    # ========================================================

    if (

        "sign in to confirm"
        in lowered

        or

        "not a bot"
        in lowered

        or

        "confirm you're not a bot"
        in lowered

        or

        "confirm you’re not a bot"
        in lowered
    ):

        return DownloadError(
            "YouTube rejected the request "
            "as automated traffic."
        )

    # ========================================================
    # AUTHENTICATION
    # ========================================================

    if (

        "authentication"
        in lowered

        or

        "cookies"
        in lowered

        or

        "sign in"
        in lowered
    ):

        return DownloadError(
            "YouTube requires authentication "
            "for this request."
        )

    # ========================================================
    # PRIVATE / UNAVAILABLE
    # ========================================================

    if (

        "private video"
        in lowered

        or

        "video unavailable"
        in lowered

        or

        "this video is not available"
        in lowered
    ):

        return VideoUnavailableError(
            "This YouTube video is "
            "unavailable or private."
        )

    # ========================================================
    # AGE / MEMBERS
    # ========================================================

    if (

        "age-restricted"
        in lowered

        or

        "confirm your age"
        in lowered

        or

        "members-only"
        in lowered
    ):

        return VideoUnavailableError(
            "This video requires "
            "authentication or has "
            "an age restriction."
        )

    # ========================================================
    # FORBIDDEN
    # ========================================================

    if (

        "http error 403"
        in lowered

        or

        "forbidden"
        in lowered
    ):

        return DownloadError(
            "YouTube rejected the media "
            "request."
        )

    # ========================================================
    # GENERIC
    # ========================================================

    return DownloadError(
        message[:700]
    )


# ============================================================
# FIND AUDIO FILE
# ============================================================

def find_audio_file(
    output_dir: Path,
    video_id: str,
):

    audio_extensions = {

        ".mp3",
        ".m4a",
        ".aac",
        ".opus",
        ".ogg",
        ".wav",
        ".flac",
        ".webm",
    }

    files = [

        file

        for file in output_dir.iterdir()

        if (

            file.is_file()

            and file.suffix.lower()
            in audio_extensions
        )
    ]

    if not files:

        return None

    return max(
        files,
        key=lambda x:
            x.stat().st_mtime,
    )


# ============================================================
# SHOULD TRY FALLBACK?
# ============================================================

def should_try_fallback(
    error: Exception,
) -> bool:

    message = str(
        error
    ).lower()

    # Do not waste time switching clients when the problem
    # is authentication, rate limiting or availability.

    blocked_patterns = [

        "rate-limit",

        "rate limit",

        "http error 429",

        "too many requests",

        "sign in",

        "not a bot",

        "confirm you're not a bot",

        "confirm you’re not a bot",

        "authentication",

        "private video",

        "video unavailable",

        "age-restricted",

        "members-only",
    ]

    for pattern in blocked_patterns:

        if pattern in message:

            return False

    fallback_patterns = [

        "http error 403",

        "forbidden",

        "requested format is not available",

        "format is not available",

        "unable to download",

        "unable to extract",

        "no video formats",

        "no formats",
    ]

    for pattern in fallback_patterns:

        if pattern in message:

            return True

    return False


# ============================================================
# SINGLE CLIENT DOWNLOAD
# ============================================================

def download_with_client(
    url: str,
    job_dir: Path,
    progress_callback=None,
    player_client: str = "mweb",
):

    tracker = ProgressTracker(
        progress_callback
    )

    options = build_ydl_options(
        output_dir=job_dir,
        progress_tracker=tracker,
        player_client=player_client,
    )

    print(
        "============================================"
    )

    print(
        "YouTube client:",
        player_client,
    )

    print(
        "YouTube download:",
        url,
    )

    print(
        "Cookies:",

        "enabled"

        if RUNTIME_COOKIE_FILE.exists()

        else "disabled",
    )

    print(
        "Target audio:",

        f"{AUDIO_OUTPUT.upper()} "
        f"{AUDIO_QUALITY} kbps",
    )

    print(
        "============================================"
    )

    # ========================================================
    # SINGLE EXTRACTION + DOWNLOAD
    # ========================================================
    #
    # Important:
    #
    # We intentionally do NOT do:
    #
    #   extract_info(download=False)
    #
    # followed by:
    #
    #   download()
    #
    # because that causes unnecessary repeated extraction.
    #
    # Instead:
    #
    #   extract_info(download=True)
    #
    # does extraction and downloading in one yt-dlp run.

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True,
            )

    except yt_dlp.utils.DownloadError as error:

        raise classify_download_error(
            error
        ) from error

    if not info:

        raise VideoUnavailableError(
            "YouTube did not return "
            "video information."
        )

    # ========================================================
    # METADATA
    # ========================================================

    video_id = (
        info.get("id")
        or "audio"
    )

    original_title = (

        info.get("track")

        or info.get("title")

        or "Audio"
    )

    # The filename is based on the actual YouTube title.
    filename_title = sanitize_filename(
        original_title
    )

    artist = (

        info.get("artist")

        or info.get("creator")

        or info.get("uploader")

        or ""
    )

    uploader = (
        info.get("uploader")
        or ""
    )

    duration = info.get(
        "duration"
    )

    # ========================================================
    # SOURCE QUALITY
    # ========================================================

    source_quality = (
        get_source_quality(
            info
        )
    )

    source_bitrate = (
        source_quality.get(
            "source_bitrate"
        )
    )

    source_codec = (
        source_quality.get(
            "source_codec"
        )
    )

    source_ext = (
        source_quality.get(
            "source_ext"
        )
    )

    # ========================================================
    # FIND DOWNLOADED AUDIO
    # ========================================================

    audio_path = find_audio_file(
        job_dir,
        video_id,
    )

    if not audio_path:

        candidates = [

            file

            for file in job_dir.iterdir()

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
                }
            )
        ]

        if candidates:

            audio_path = max(

                candidates,

                key=lambda x:
                    x.stat().st_mtime,
            )

    if not audio_path:

        raise DownloadError(
            "Audio was downloaded but "
            "the final audio file could "
            "not be located."
        )

    # ========================================================
    # FINAL FILENAME
    # ========================================================
    #
    # This is the important change.
    #
    # Whatever temporary filename yt-dlp created, we rename
    # the FINAL processed audio to:
    #
    #     YouTube Title.mp3
    #
    # This guarantees Telegram receives the human-readable
    # title as the filename.

    final_extension = (
        f".{AUDIO_OUTPUT}"
    )

    final_filename = (
        f"{filename_title}"
        f"{final_extension}"
    )

    final_path = (
        job_dir
        / final_filename
    )

    # Avoid accidentally moving a file onto itself.
    try:

        if audio_path.resolve() != final_path.resolve():

            if final_path.exists():

                final_path.unlink()

            audio_path.rename(
                final_path
            )

            audio_path = final_path

    except Exception as error:

        print(
            "Filename rename failed:",
            type(error).__name__,
            str(error),
        )

        # If rename fails, keep the original file rather
        # than breaking an otherwise successful download.

    # ========================================================
    # ACTUAL OUTPUT QUALITY
    # ========================================================

    output_quality = (
        get_audio_quality(
            audio_path
        )
    )

    output_bitrate = (
        output_quality.get(
            "output_bitrate"
        )
    )

    quality_text = (
        output_quality.get(
            "quality_text"
        )
    )

    if not quality_text:

        quality_text = (

            f"{AUDIO_OUTPUT.upper()} • "
            f"{AUDIO_QUALITY} kbps"
        )

    # ========================================================
    # FILE SIZE
    # ========================================================

    file_size = (
        audio_path.stat().st_size
    )

    if file_size <= 0:

        raise DownloadError(
            "Downloaded audio file is empty."
        )

    if (

        file_size
        > MAX_FILE_SIZE_BYTES
    ):

        raise FileTooLargeError(

            "The resulting audio file "
            f"is {format_bytes(file_size)}, "
            "which exceeds the configured "
            f"{MAX_FILE_SIZE_MB} MB limit."
        )

    # ========================================================
    # FINAL PROGRESS
    # ========================================================

    if progress_callback:

        progress_callback(
            {

                "status":
                    "Audio ready",

                "percent":
                    100,

                "quality":
                    quality_text,

                "output_bitrate":
                    output_bitrate,

                "source_bitrate":
                    source_bitrate,

                "filename":
                    audio_path.name,
            }
        )

    # ========================================================
    # LOG
    # ========================================================

    print(
        "============================================"
    )

    print(
        "Audio ready:"
    )

    print(
        "Title:",
        original_title,
    )

    print(
        "Filename:",
        audio_path.name,
    )

    print(
        "Source:",
        f"{source_ext} / {source_codec}",
    )

    print(
        "Source bitrate:",

        (
            f"{source_bitrate} kbps"

            if source_bitrate

            else "Unknown"
        ),
    )

    print(
        "Output:",
        quality_text,
    )

    print(
        "File size:",
        format_bytes(
            file_size
        ),
    )

    print(
        "============================================"
    )

    # ========================================================
    # RESULT
    # ========================================================

    return {

        "path":
            audio_path,

        "job_dir":
            job_dir,

        "filename":
            audio_path.name,

        "title":
            original_title,

        "artist":
            artist,

        "uploader":
            uploader,

        "duration":
            duration,

        # ----------------------------------------------------
        # SOURCE QUALITY
        # ----------------------------------------------------

        "source_codec":
            source_codec,

        "source_ext":
            source_ext,

        "source_bitrate":
            source_bitrate,

        # ----------------------------------------------------
        # OUTPUT QUALITY
        # ----------------------------------------------------

        "output_codec":
            output_quality.get(
                "output_codec"
            ),

        "output_bitrate":
            output_bitrate,

        "sample_rate":
            output_quality.get(
                "sample_rate"
            ),

        "channels":
            output_quality.get(
                "channels"
            ),

        "quality":
            quality_text,

        "quality_text":
            quality_text,

        # ----------------------------------------------------
        # FILE
        # ----------------------------------------------------

        "file_size":
            file_size,
    }


# ============================================================
# ASYNC DOWNLOAD
# ============================================================

async def download_audio(
    url: str,
    output_dir,
    progress_callback=None,
):

    ensure_ffmpeg()

    verify_cookie_file()

    prepare_cookie_file()

    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # CLIENT ORDER
    # ========================================================

    clients = [

        "mweb",

        "web_safari",

        "android_vr",

        "web_embedded",
    ]

    loop = asyncio.get_running_loop()

    # ========================================================
    # CALLBACK BRIDGE
    # ========================================================
    #
    # yt-dlp executes progress hooks synchronously.
    #
    # Telegram callbacks are asynchronous.
    #
    # This safely bridges the two.

    def thread_safe_callback(
        progress,
    ):

        if not progress_callback:

            return

        try:

            result = progress_callback(
                progress
            )

            if inspect.isawaitable(
                result
            ):

                asyncio.run_coroutine_threadsafe(

                    result,

                    loop,
                )

        except Exception as error:

            print(
                "Progress bridge error:",
                type(error).__name__,
            )

    # ========================================================
    # SEMAPHORE
    # ========================================================

    async with _download_semaphore:

        for index, client in enumerate(
            clients
        ):

            job_dir = Path(
                tempfile.mkdtemp(
                    prefix="audio_",
                    dir=str(
                        output_dir
                    ),
                )
            )

            try:

                # ------------------------------------------------
                # STATUS
                # ------------------------------------------------

                thread_safe_callback(
                    {

                        "status":

                            (

                                "Connecting to YouTube"

                                if index == 0

                                else

                                f"Retrying with {client}"
                            ),

                        "percent":
                            None,
                    }
                )

                # ------------------------------------------------
                # DOWNLOAD
                # ------------------------------------------------

                result = await asyncio.wait_for(

                    asyncio.get_running_loop()
                    .run_in_executor(

                        None,

                        lambda: (

                            download_with_client(

                                url=url,

                                job_dir=job_dir,

                                progress_callback=
                                    thread_safe_callback,

                                player_client=
                                    client,
                            )
                        ),
                    ),

                    timeout=DOWNLOAD_TIMEOUT,
                )

                print(
                    "Download successful "
                    f"using client: {client}"
                )

                return result

            except (

                FileTooLargeError,

                DurationTooLongError,

                VideoUnavailableError,

            ):

                shutil.rmtree(

                    job_dir,

                    ignore_errors=True,
                )

                raise

            except DownloadError as error:

                print(

                    f"Client {client} failed:",

                    str(error),
                )

                shutil.rmtree(

                    job_dir,

                    ignore_errors=True,
                )

                # ------------------------------------------------
                # DON'T WASTE TIME ON BLOCKING ERRORS
                # ------------------------------------------------

                if not should_try_fallback(
                    error
                ):

                    raise

                # ------------------------------------------------
                # LAST CLIENT
                # ------------------------------------------------

                if index >= (
                    len(clients) - 1
                ):

                    raise

                # ------------------------------------------------
                # SHORT FALLBACK DELAY
                # ------------------------------------------------

                if FALLBACK_DELAY > 0:

                    await asyncio.sleep(
                        FALLBACK_DELAY
                    )

            except asyncio.TimeoutError:

                print(
                    f"Client {client} "
                    "timed out."
                )

                shutil.rmtree(

                    job_dir,

                    ignore_errors=True,
                )

                raise DownloadError(
                    "The download timed out."
                )

            except Exception as error:

                print(

                    f"Unexpected error with "
                    f"{client}:",

                    type(error).__name__,

                    str(error),
                )

                shutil.rmtree(

                    job_dir,

                    ignore_errors=True,
                )

                if index >= (
                    len(clients) - 1
                ):

                    raise DownloadError(
                        str(error)[:700]
                    ) from error

                if FALLBACK_DELAY > 0:

                    await asyncio.sleep(
                        FALLBACK_DELAY
                    )

        raise DownloadError(
            "All YouTube download clients failed."
        )


# ============================================================
# CLEANUP
# ============================================================

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
            type(error).__name__,
        )
