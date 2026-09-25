import os
import re
import asyncio
import shutil
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

RETRY_BACKOFF = max(
    3,
    int(
        os.getenv(
            "RETRY_BACKOFF",
            "8"
        )
    )
)


# ============================================================
# YOUTUBE COOKIES
# ============================================================

# Render Secret File:
#
# Render Dashboard
#     ↓
# Environment
#     ↓
# Secret Files
#     ↓
# cookies.txt
#
# Render exposes it as:
#
# /etc/secrets/cookies.txt
#
# NEVER put this file in GitHub.
# NEVER print its contents.
# NEVER log cookie values.

COOKIE_FILE = Path(
    os.getenv(
        "YOUTUBE_COOKIE_FILE",
        "/etc/secrets/cookies.txt"
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


def verify_cookie_file():

    if cookies_available():

        print(
            "YouTube cookies: available"
        )

        return

    print(
        "YouTube cookies: NOT FOUND"
    )

    print(
        f"Expected cookie file: "
        f"{COOKIE_FILE}"
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


class FileTooLargeError(
    DownloadError
):
    pass


class DurationTooLongError(
    DownloadError
):
    pass


class VideoUnavailableError(
    DownloadError
):
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

    if not shutil.which(
        "ffmpeg"
    ):

        raise DownloadError(
            "FFmpeg is not installed on "
            "the server."
        )


def format_bytes(
    size: Optional[int],
) -> str:

    if size is None:

        return "Unknown"

    size = float(
        size
    )

    for unit in (
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ):

        if size < 1024:

            return (
                f"{size:.1f} {unit}"
            )

        size /= 1024

    return (
        f"{size:.1f} TB"
    )


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

    def hook(
        self,
        data,
    ):

        if not self.callback:

            return

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

            if not total:

                return

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

            if (
                self.last_percent < 0
                or (
                    percent
                    - self.last_percent
                    >= 2
                )
                or percent >= 99
            ):

                self.last_percent = (
                    percent
                )

                self.callback(
                    {
                        "status":
                            "Downloading",

                        "percent":
                            percent,

                        "downloaded_bytes":
                            downloaded,

                        "total_bytes":
                            total,
                    }
                )

        elif status == "finished":

            self.callback(
                {
                    "status":
                        "Processing audio",

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
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_dir: Path,
    progress_tracker: ProgressTracker,
    player_client: str,
):

    output_template = str(
        output_dir
        / "%(id)s.%(ext)s"
    )

    options = {

        # ====================================================
        # FORMAT
        # ====================================================

        "format":
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/best",

        "outtmpl":
            output_template,

        "noplaylist":
            True,

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
        # REQUEST PACING
        # ====================================================

        "sleep_interval_requests":
            2,

        "sleep_interval":
            2,

        "max_sleep_interval":
            6,

        # ====================================================
        # PROGRESS
        # ====================================================

        "progress_hooks": [
            progress_tracker.hook
        ],

        # ====================================================
        # METADATA
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

    if cookies_available():

        options[
            "cookiefile"
        ] = str(
            COOKIE_FILE
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

    # --------------------------------------------------------
    # RATE LIMIT
    # --------------------------------------------------------

    if (
        "http error 429"
        in lowered
        or "too many requests"
        in lowered
        or "rate limit"
        in lowered
    ):

        return DownloadError(
            "YouTube is temporarily "
            "rate-limiting this server."
        )

    # --------------------------------------------------------
    # BOT CHECK
    # --------------------------------------------------------

    if (
        "sign in to confirm"
        in lowered
        or "not a bot"
        in lowered
        or "confirm you're not a bot"
        in lowered
        or "confirm you’re not a bot"
        in lowered
    ):

        return DownloadError(
            "YouTube rejected the request "
            "as automated traffic."
        )

    # --------------------------------------------------------
    # COOKIES / AUTHENTICATION
    # --------------------------------------------------------

    if (
        "authentication"
        in lowered
        or "cookies"
        in lowered
        or "sign in"
        in lowered
    ):

        return DownloadError(
            "YouTube requires authentication "
            "for this request."
        )

    # --------------------------------------------------------
    # PRIVATE / UNAVAILABLE
    # --------------------------------------------------------

    if (
        "private video"
        in lowered
        or "video unavailable"
        in lowered
        or "this video is not available"
        in lowered
    ):

        return VideoUnavailableError(
            "This YouTube video is "
            "unavailable or private."
        )

    # --------------------------------------------------------
    # AGE / MEMBERS
    # --------------------------------------------------------

    if (
        "age-restricted"
        in lowered
        or "confirm your age"
        in lowered
        or "members-only"
        in lowered
    ):

        return VideoUnavailableError(
            "This video requires "
            "authentication or has "
            "an age restriction."
        )

    # --------------------------------------------------------
    # FORBIDDEN
    # --------------------------------------------------------

    if (
        "http error 403"
        in lowered
        or "forbidden"
        in lowered
    ):

        return DownloadError(
            "YouTube rejected the media "
            "request."
        )

    # --------------------------------------------------------
    # GENERIC
    # --------------------------------------------------------

    return DownloadError(
        message[:700]
    )


# ============================================================
# EXTRACT INFO
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
                "YouTube did not return "
                "video information."
            )

        return info

    except yt_dlp.utils.DownloadError as error:

        raise classify_download_error(
            error
        ) from error


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
            and file.name.startswith(
                f"{video_id}."
            )
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
        "YouTube download:"
    )

    print(url)

    print(
        "Cookies:",
        "enabled"
        if cookies_available()
        else "disabled",
    )

    print(
        "============================================"
    )

    # ========================================================
    # METADATA
    # ========================================================

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

        max_minutes = (
            MAX_DURATION // 60
        )

        raise DurationTooLongError(
            f"This audio is too long. "
            f"Maximum allowed duration is "
            f"{max_minutes} minutes."
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
        "Video ID:",
        video_id,
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
    # FIND RESULT
    # ========================================================

    audio_path = find_audio_file(
        job_dir,
        video_id,
    )

    if not audio_path:

        raise DownloadError(
            "yt-dlp completed but the "
            "audio file was not found."
        )

    # ========================================================
    # FILE SIZE
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

    if (
        file_size
        > MAX_FILE_SIZE_BYTES
    ):

        raise FileTooLargeError(
            "The final audio file is "
            f"{format_bytes(file_size)}, "
            "which exceeds the configured "
            f"{MAX_FILE_SIZE_MB} MB limit."
        )

    # ========================================================
    # RENAME
    # ========================================================

    clean_title = sanitize_filename(
        title,
        90,
    )

    extension = (
        audio_path.suffix.lower()
    )

    final_path = (
        job_dir
        / f"{clean_title}{extension}"
    )

    counter = 1

    while final_path.exists():

        final_path = (
            job_dir
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
    # RESULT
    # ========================================================

    return {

        "path":
            str(audio_path),

        "filename":
            audio_path.name,

        "title":
            str(title),

        "artist":
            str(artist),

        "uploader":
            str(uploader),

        "duration":
            duration,

        "thumbnail":
            str(thumbnail),

        "webpage_url":
            webpage_url,

        "video_id":
            video_id,

        "filesize":
            file_size,

        "format":
            extension.lstrip("."),

        "quality":
            AUDIO_QUALITY,

        "job_dir":
            str(job_dir),
    }


# ============================================================
# SYNCHRONOUS DOWNLOAD
# ============================================================

def download_sync(
    url: str,
    output_dir: Path,
    progress_callback=None,
):

    ensure_ffmpeg()

    verify_cookie_file()

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # ISOLATED JOB DIRECTORY
    # ========================================================

    job_dir = Path(
        tempfile.mkdtemp(
            prefix="job_",
            dir=str(output_dir),
        )
    )

    print(
        "Job directory:",
        job_dir,
    )

    # ========================================================
    # CLIENT FALLBACKS
    #
    # mweb:
    #   Primary client + BGUTIL
    #
    # web_safari:
    #   HLS-capable fallback
    #
    # android_vr:
    #   Does not currently require GVS PO token
    #
    # web_embedded:
    #   Does not currently require GVS PO token,
    #   but only works for embeddable videos.
    #
    # ========================================================

    clients = [
        "mweb",
        "web_safari",
        "android_vr",
        "web_embedded",
    ]

    last_error = None

    try:

        for index, client in enumerate(
            clients,
            start=1,
        ):

            try:

                print(
                    "============================================"
                )

                print(
                    f"Download attempt "
                    f"{index}/{len(clients)}"
                )

                print(
                    "Client:",
                    client,
                )

                print(
                    "============================================"
                )

                result = download_with_client(
                    url=url,
                    job_dir=job_dir,
                    progress_callback=progress_callback,
                    player_client=client,
                )

                print(
                    "SUCCESS with client:",
                    client,
                )

                return result

            except (
                FileTooLargeError,
                DurationTooLongError,
                VideoUnavailableError,
            ):

                raise

            except DownloadError as error:

                last_error = error

                print(
                    "Client failed:",
                    client,
                )

                print(
                    "Reason:",
                    error,
                )

                if index < len(clients):

                    delay = (
                        RETRY_BACKOFF
                        * index
                    )

                    print(
                        f"Waiting {delay} seconds "
                        "before next client..."
                    )

                    time.sleep(
                        delay
                    )

        if last_error:

            raise last_error

        raise DownloadError(
            "All YouTube download methods failed."
        )

    except Exception:

        # ----------------------------------------------------
        # Failed job = safe to remove completely
        # ----------------------------------------------------

        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        raise


# ============================================================
# ASYNC WRAPPER
# ============================================================

async def download_audio(
    url: str,
    output_dir: Path,
    progress_callback=None,
):

    async with _download_semaphore:

        loop = (
            asyncio.get_running_loop()
        )

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
                "The download took too long. "
                "Please try again later."
            )
