import os
import re
import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp


# ============================================================
# CONFIGURATION
# ============================================================

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

MAX_DURATION = int(os.getenv("MAX_DURATION", "7200"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "600"))

AUDIO_OUTPUT = os.getenv("AUDIO_OUTPUT", "mp3").lower()
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "192")

DOWNLOAD_RETRIES = int(os.getenv("DOWNLOAD_RETRIES", "3"))


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

def sanitize_filename(filename: str, max_length: int = 120) -> str:
    filename = str(filename or "audio")

    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    filename = re.sub(r'[\\/:*?"<>|]+', "_", filename)
    filename = re.sub(r"\s+", " ", filename)

    filename = filename.strip(" .")

    if not filename:
        filename = "audio"

    return filename[:max_length]


def ensure_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise DownloadError(
            "FFmpeg is not installed on the server."
        )


def format_bytes(size: Optional[int]) -> str:
    if size is None:
        return "Unknown"

    size = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} TB"


# ============================================================
# PROGRESS
# ============================================================

class ProgressTracker:

    def __init__(self, callback=None):
        self.callback = callback
        self.last_percent = -1

    def hook(self, data):

        if not self.callback:
            return

        status = data.get("status")

        if status == "downloading":

            downloaded = data.get(
                "downloaded_bytes",
                0,
            )

            total = (
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
            )

            percent = None

            if total:
                percent = downloaded / total * 100

                percent = max(
                    0,
                    min(100, percent),
                )

            if (
                percent is not None
                and (
                    self.last_percent < 0
                    or percent - self.last_percent >= 2
                    or percent >= 99
                )
            ):

                self.last_percent = percent

                self.callback({
                    "status": "Downloading",
                    "percent": percent,
                    "downloaded_bytes": downloaded,
                    "total_bytes": total,
                })

        elif status == "finished":

            self.callback({
                "status": "Download complete",
                "percent": 100,
            })

        elif status == "error":

            self.callback({
                "status": "Download failed",
                "percent": None,
            })


# ============================================================
# LOGGER
# ============================================================

class YTDLPLogger:

    def debug(self, message):

        if message.startswith("[debug]"):
            return

        print("yt-dlp:", message)

    def warning(self, message):
        print("yt-dlp warning:", message)

    def error(self, message):
        print("yt-dlp error:", message)


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_dir: Path,
    progress_tracker: ProgressTracker,
):

    output_template = str(
        output_dir / "%(id)s.%(ext)s"
    )

    return {

        # ----------------------------------------------------
        # AUDIO SOURCE
        # ----------------------------------------------------

        "format":
            "bestaudio[ext=m4a]/"
            "bestaudio[ext=webm]/"
            "bestaudio/best",

        "outtmpl": output_template,

        "noplaylist": True,

        # ----------------------------------------------------
        # OUTPUT
        # ----------------------------------------------------

        "quiet": True,
        "no_warnings": False,
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
        # METADATA
        # ----------------------------------------------------

        "writethumbnail": True,
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
        # BGUTIL PO TOKEN PROVIDER
        # ----------------------------------------------------

        "extractor_args": {

            "youtubepot-bgutilhttp": {

                "base_url":
                    "http://127.0.0.1:4416"
            }
        },

        # ----------------------------------------------------
        # JAVASCRIPT RUNTIME
        #
        # IMPORTANT:
        # yt-dlp Python API expects:
        #
        # runtime -> configuration dictionary
        # ----------------------------------------------------

        "js_runtimes": {

            "deno": {

                "path":
                    "/root/.deno/bin/deno"
            }
        },

        # ----------------------------------------------------
        # REMOTE EJS COMPONENTS
        # ----------------------------------------------------

        "remote_components": {
            "ejs:npm"
        },

        # ----------------------------------------------------
        # USER AGENT
        # ----------------------------------------------------

        "http_headers": {

            "User-Agent":
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
        },

        # ----------------------------------------------------
        # LOGGER
        # ----------------------------------------------------

        "logger": YTDLPLogger(),
    }


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def classify_download_error(error):

    message = str(error)
    lowered = message.lower()

    if (
        "private video" in lowered
        or "video unavailable" in lowered
        or "this video is not available" in lowered
    ):

        return VideoUnavailableError(
            "This YouTube video is unavailable "
            "or private."
        )

    if (
        "age-restricted" in lowered
        or "confirm your age" in lowered
        or "members-only" in lowered
    ):

        return VideoUnavailableError(
            "This video requires YouTube "
            "authentication or has an age "
            "restriction."
        )

    if (
        "sign in to confirm" in lowered
        or "not a bot" in lowered
    ):

        return DownloadError(
            "YouTube rejected this request. "
            "The automatic PO-token provider "
            "could not satisfy the current "
            "YouTube check."
        )

    if (
        "http error 403" in lowered
        or "forbidden" in lowered
    ):

        return DownloadError(
            "YouTube rejected the media request. "
            "Please try again."
        )

    return DownloadError(
        message[:700]
    )


# ============================================================
# EXTRACT INFORMATION
# ============================================================

def extract_info(url: str, options: dict):

    try:

        with yt_dlp.YoutubeDL(options) as ydl:

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

        raise classify_download_error(error) from error


# ============================================================
# FIND AUDIO
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

    exact = [

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

    if exact:

        return max(
            exact,
            key=lambda x:
                x.stat().st_mtime,
        )

    return None


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

        if (
            keep
            and file.resolve()
            == keep.resolve()
        ):
            continue

        delete = False

        if video_id:

            if file.name.startswith(
                f"{video_id}."
            ):
                delete = True

        if (
            file.suffix.lower()
            in temporary_extensions
        ):
            delete = True

        if delete:

            try:
                file.unlink()

            except Exception as error:

                print(
                    "Cleanup error:",
                    error,
                )


# ============================================================
# SYNCHRONOUS DOWNLOAD
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

    tracker = ProgressTracker(
        progress_callback
    )

    options = build_ydl_options(
        job_dir,
        tracker,
    )

    print(
        "============================================"
    )

    print("YouTube download:")
    print(url)

    print(
        "============================================"
    )

    try:

        # ----------------------------------------------------
        # GET METADATA
        # ----------------------------------------------------

        info = extract_info(
            url,
            options,
        )

        duration = info.get("duration")

        if (
            duration
            and duration > MAX_DURATION
        ):

            max_minutes = MAX_DURATION // 60

            raise DurationTooLongError(
                f"This audio is too long. "
                f"Maximum allowed duration is "
                f"{max_minutes} minutes."
            )

        # ----------------------------------------------------
        # METADATA
        # ----------------------------------------------------

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

        print("Title:", title)
        print("Artist:", artist)
        print("Video ID:", video_id)

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        try:

            with yt_dlp.YoutubeDL(options) as ydl:

                ydl.download([url])

        except yt_dlp.utils.DownloadError as error:

            raise classify_download_error(
                error
            ) from error

        # ----------------------------------------------------
        # FIND AUDIO
        # ----------------------------------------------------

        audio_path = find_audio_file(
            job_dir,
            video_id,
        )

        if not audio_path:

            raise DownloadError(
                "yt-dlp completed but the "
                "audio file was not found."
            )

        # ----------------------------------------------------
        # FILE SIZE
        # ----------------------------------------------------

        file_size = audio_path.stat().st_size

        print(
            "Final file:",
            audio_path,
        )

        print(
            "Final size:",
            format_bytes(file_size),
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

        # ----------------------------------------------------
        # RENAME
        # ----------------------------------------------------

        clean_title = sanitize_filename(
            title,
            90,
        )

        extension = audio_path.suffix.lower()

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

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

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

    except Exception:

        try:

            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )

        except Exception:
            pass

        raise


# ============================================================
# ASYNC WRAPPER
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
            "The download took too long. "
            "Please try again later."
        )