"""
Spotify integration for Audio Bot.

API:
    https://spotify-api40.p.rapidapi.com

Supported endpoints:
    GET /search
    GET /track

Environment variables:
    SPOTIFY_API40_KEY
    SPOTIFY_API40_HOST   (optional; defaults to spotify-api40.p.rapidapi.com)

The public functions are intentionally kept compatible with bot.py:

    search_spotify(query, limit=8)
    download_spotify_song(track_id, output_dir)

Both functions return dictionaries in the format expected by bot.py.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


# ============================================================
# Configuration
# ============================================================

SPOTIFY_API40_KEY = os.getenv("SPOTIFY_API40_KEY", "").strip()

SPOTIFY_API40_HOST = os.getenv(
    "SPOTIFY_API40_HOST",
    "spotify-api40.p.rapidapi.com",
).strip()

SPOTIFY_API40_BASE_URL = f"https://{SPOTIFY_API40_HOST}"

SEARCH_ENDPOINT = f"{SPOTIFY_API40_BASE_URL}/search"
TRACK_ENDPOINT = f"{SPOTIFY_API40_BASE_URL}/track"

REQUEST_TIMEOUT = int(os.getenv("SPOTIFY_API40_TIMEOUT", "60"))
DOWNLOAD_TIMEOUT = int(os.getenv("SPOTIFY_API40_DOWNLOAD_TIMEOUT", "300"))

USER_AGENT = "Audio-Bot/3.0"

MAX_FILENAME_LENGTH = 180


# ============================================================
# Exceptions
# ============================================================


class SpotifyError(Exception):
    """Base Spotify error."""


class SpotifyAPIError(SpotifyError):
    """Spotify API request failed."""


class SpotifyDownloadError(SpotifyError):
    """Spotify audio download failed."""


# ============================================================
# HTTP session
# ============================================================


session = requests.Session()

session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
)


def _headers() -> dict[str, str]:
    """
    Headers required by RapidAPI.
    """
    if not SPOTIFY_API40_KEY:
        raise SpotifyAPIError(
            "SPOTIFY_API40_KEY is not configured."
        )

    return {
        "x-rapidapi-key": SPOTIFY_API40_KEY,
        "x-rapidapi-host": SPOTIFY_API40_HOST,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


# ============================================================
# Generic helpers
# ============================================================


def _safe_json(response: requests.Response) -> Any:
    """
    Safely decode a JSON response.
    """
    try:
        return response.json()
    except ValueError:
        return None


def _request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> Any:
    """
    Perform an API request and return decoded JSON.
    """

    try:
        response = session.request(
            method=method,
            url=url,
            headers=_headers(),
            params=params,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SpotifyAPIError(
            f"Spotify API request failed: {exc}"
        ) from exc

    data = _safe_json(response)

    if response.status_code >= 400:
        detail = ""

        if isinstance(data, dict):
            detail = (
                data.get("message")
                or data.get("error")
                or data.get("detail")
                or ""
            )

        if not detail:
            detail = response.text[:500]

        raise SpotifyAPIError(
            f"Spotify API returned HTTP {response.status_code}: {detail}"
        )

    if data is None:
        raise SpotifyAPIError(
            "Spotify API returned a non-JSON response."
        )

    return data


def _string(value: Any) -> str:
    """
    Convert a value to a clean string.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    return str(value).strip()


def _first_value(
    obj: Any,
    keys: tuple[str, ...],
) -> Any:
    """
    Recursively search an arbitrary JSON structure for one
    of the supplied keys.

    This is intentionally tolerant because RapidAPI products
    sometimes change nesting between response versions.
    """

    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]

        for value in obj.values():
            result = _first_value(value, keys)

            if result not in (None, ""):
                return result

    elif isinstance(obj, list):
        for item in obj:
            result = _first_value(item, keys)

            if result not in (None, ""):
                return result

    return None


def _find_values(
    obj: Any,
    keys: tuple[str, ...],
) -> list[Any]:
    """
    Recursively find every value associated with one of the
    supplied keys.
    """

    found: list[Any] = []

    if isinstance(obj, dict):
        for key, value in obj.items():

            if key in keys:
                found.append(value)

            found.extend(_find_values(value, keys))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_values(item, keys))

    return found


def _is_http_url(value: Any) -> bool:
    """
    Check whether a value looks like an HTTP/HTTPS URL.
    """

    if not isinstance(value, str):
        return False

    value = value.strip()

    if not value:
        return False

    try:
        parsed = urlparse(value)

        return parsed.scheme in ("http", "https") and bool(
            parsed.netloc
        )

    except Exception:
        return False


def _normalize_track_id(value: Any) -> str:
    """
    Convert:

        Spotify ID
        Spotify track URL
        spotify:track:ID

    into a plain Spotify track ID.
    """

    value = _string(value)

    if not value:
        return ""

    # spotify:track:xxxxxxxx
    match = re.search(
        r"spotify:track:([A-Za-z0-9]+)",
        value,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # https://open.spotify.com/track/xxxxxxxx
    match = re.search(
        r"open\.spotify\.com/track/([A-Za-z0-9]+)",
        value,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # Remove query parameters/fragments from a plain URL.
    if "?" in value:
        value = value.split("?", 1)[0]

    if "#" in value:
        value = value.split("#", 1)[0]

    # If it is already an ID.
    if re.fullmatch(r"[A-Za-z0-9]{10,64}", value):
        return value

    return value


# ============================================================
# Spotify URL helpers
# ============================================================


def is_spotify_url(text: str) -> bool:
    """
    Return True when text contains a Spotify URL.
    """

    if not text:
        return False

    return bool(
        re.search(
            r"https?://(?:open\.)?spotify\.com/",
            text,
            flags=re.IGNORECASE,
        )
    )


def extract_spotify_track_id(text: str) -> str | None:
    """
    Extract a Spotify track ID from a URL or URI.
    """

    if not text:
        return None

    # Standard URL.
    match = re.search(
        r"spotify\.com/track/([A-Za-z0-9]+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # Spotify URI.
    match = re.search(
        r"spotify:track:([A-Za-z0-9]+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return None


# ============================================================
# Artist / title normalization
# ============================================================


def _extract_artist(value: Any) -> str:
    """
    Normalize artist information from different possible
    Spotify response shapes.
    """

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        # Common direct names.
        for key in (
            "name",
            "artistName",
            "artist_name",
            "title",
        ):
            if value.get(key):
                return _string(value[key])

        # Nested artist.
        for key in (
            "artist",
            "artists",
        ):
            if key in value:
                result = _extract_artist(value[key])

                if result:
                    return result

    if isinstance(value, list):
        artists: list[str] = []

        for item in value:
            name = _extract_artist(item)

            if name and name not in artists:
                artists.append(name)

        return ", ".join(artists)

    return ""


def _extract_title(obj: Any) -> str:
    value = _first_value(
        obj,
        (
            "title",
            "trackName",
            "track_name",
            "name",
            "songName",
            "song_name",
        ),
    )

    return _string(value)


def _extract_artist_from_object(obj: Any) -> str:
    value = _first_value(
        obj,
        (
            "artists",
            "artist",
            "artistName",
            "artist_name",
            "performer",
            "performers",
        ),
    )

    return _extract_artist(value)


def _extract_album(obj: Any) -> str:
    value = _first_value(
        obj,
        (
            "albumName",
            "album_name",
            "album",
        ),
    )

    if isinstance(value, dict):
        return _string(
            value.get("name")
            or value.get("title")
            or ""
        )

    return _string(value)


def _extract_duration(obj: Any) -> int | None:
    value = _first_value(
        obj,
        (
            "duration",
            "duration_ms",
            "durationMs",
            "durationMillis",
            "duration_millis",
        ),
    )

    if value is None:
        return None

    # Sometimes duration is nested.
    if isinstance(value, dict):
        value = (
            value.get("milliseconds")
            or value.get("ms")
            or value.get("value")
        )

    try:
        duration = float(value)

        # Spotify usually reports milliseconds.
        if duration > 10000:
            return int(duration / 1000)

        return int(duration)

    except (TypeError, ValueError):
        return None


def _extract_artwork(obj: Any) -> str | None:
    """
    Try to locate album artwork.
    """

    candidates = _find_values(
        obj,
        (
            "artwork",
            "artworkUrl",
            "artwork_url",
            "cover",
            "coverUrl",
            "cover_url",
            "image",
            "imageUrl",
            "image_url",
            "thumbnail",
            "thumbnailUrl",
            "thumbnail_url",
        ),
    )

    for candidate in candidates:

        if _is_http_url(candidate):
            return candidate

        if isinstance(candidate, dict):
            nested = _first_value(
                candidate,
                (
                    "url",
                    "href",
                    "src",
                ),
            )

            if _is_http_url(nested):
                return nested

        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, str) and _is_http_url(item):
                    return item

                if isinstance(item, dict):
                    nested = _first_value(
                        item,
                        (
                            "url",
                            "href",
                            "src",
                        ),
                    )

                    if _is_http_url(nested):
                        return nested

    return None


# ============================================================
# Search response parsing
# ============================================================


def _looks_like_track(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False

    track_id = _first_value(
        obj,
        (
            "id",
            "trackId",
            "track_id",
            "spotifyId",
            "spotify_id",
        ),
    )

    title = _extract_title(obj)

    return bool(track_id and title)


def _find_track_list(obj: Any) -> list[Any]:
    """
    Recursively locate the most likely list containing tracks.
    """

    preferred_keys = (
        "tracks",
        "trackResults",
        "track_results",
        "songs",
        "items",
        "results",
        "data",
    )

    if isinstance(obj, dict):

        # First inspect preferred keys.
        for key in preferred_keys:

            if key not in obj:
                continue

            value = obj[key]

            if isinstance(value, list):
                if any(_looks_like_track(x) for x in value):
                    return value

            if isinstance(value, dict):
                nested = _find_track_list(value)

                if nested:
                    return nested

        # Then recursively inspect everything.
        for value in obj.values():

            nested = _find_track_list(value)

            if nested:
                return nested

    elif isinstance(obj, list):

        if any(_looks_like_track(x) for x in obj):
            return obj

        for item in obj:

            nested = _find_track_list(item)

            if nested:
                return nested

    return []


def _normalize_track(item: Any) -> dict[str, Any]:
    """
    Convert an arbitrary API track object into the format
    consumed by bot.py.
    """

    if not isinstance(item, dict):
        return {}

    track_id = _first_value(
        item,
        (
            "id",
            "trackId",
            "track_id",
            "spotifyId",
            "spotify_id",
        ),
    )

    title = _extract_title(item)
    artist = _extract_artist_from_object(item)
    album = _extract_album(item)
    duration = _extract_duration(item)
    artwork = _extract_artwork(item)

    track_url = _first_value(
        item,
        (
            "url",
            "trackUrl",
            "track_url",
            "spotifyUrl",
            "spotify_url",
            "externalUrl",
        ),
    )

    if isinstance(track_url, dict):
        track_url = _first_value(
            track_url,
            (
                "spotify",
                "url",
                "href",
            ),
        )

    return {
        "id": _normalize_track_id(track_id),
        "track_id": _normalize_track_id(track_id),
        "spotify_id": _normalize_track_id(track_id),
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artist,
        "album": album,
        "duration": duration,
        "artwork": artwork,
        "image": artwork,
        "url": _string(track_url),
        "raw": item,
    }


# ============================================================
# Spotify search
# ============================================================


def search_spotify(
    query: str,
    limit: int = 8,
) -> dict[str, Any]:
    """
    Search Spotify through spotify-api40.

    Compatible with current bot.py.

    Returns:

    {
        "query": "...",
        "results": [...],
        "raw": {...}
    }
    """

    query = _string(query)

    if not query:
        return {
            "query": "",
            "results": [],
            "raw": {},
        }

    limit = max(1, min(int(limit), 50))

    params = {
        "query": query,
    }

    # /search in this API requires "query".
    data = _request(
        "GET",
        SEARCH_ENDPOINT,
        params=params,
    )

    raw_tracks = _find_track_list(data)

    results: list[dict[str, Any]] = []

    for item in raw_tracks:

        normalized = _normalize_track(item)

        if not normalized:
            continue

        if not normalized.get("id"):
            continue

        results.append(normalized)

        if len(results) >= limit:
            break

    return {
        "query": query,
        "results": results,
        "raw": data,
    }


# ============================================================
# /track response parsing
# ============================================================


AUDIO_URL_KEYS = (
    # Most obvious names.
    "downloadUrl",
    "download_url",
    "download",
    "audioUrl",
    "audio_url",
    "audio",
    "audioDownloadUrl",
    "audio_download_url",

    # Common API naming.
    "fileUrl",
    "file_url",
    "file",
    "mediaUrl",
    "media_url",
    "media",
    "streamUrl",
    "stream_url",
    "stream",

    # CDN/file names.
    "url",
    "href",
    "link",
    "src",

    # Possible explicit Spotify downloader names.
    "downloadLink",
    "download_link",
    "audioLink",
    "audio_link",
    "playUrl",
    "play_url",
    "playbackUrl",
    "playback_url",
)


def _score_audio_url(url: str) -> int:
    """
    Give likely audio URLs a score.

    Higher score = more likely to be an actual downloadable
    audio file rather than a Spotify metadata page.
    """

    if not _is_http_url(url):
        return -1000

    lower = url.lower()

    score = 0

    # Explicit audio/file indicators.
    for token in (
        ".mp3",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wav",
        ".flac",
        ".webm",
        ".audio",
        "audio",
        "download",
        "cdn",
        "media",
        "stream",
    ):
        if token in lower:
            score += 10

    # Spotify page is NOT the audio file.
    if "open.spotify.com/track/" in lower:
        score -= 100

    if "spotify.com" in lower and "download" not in lower:
        score -= 50

    return score


def _find_audio_url(obj: Any) -> str | None:
    """
    Recursively find the most likely actual audio/download URL
    in the /track response.

    Handles:

        {"audio": "..."}
        {"downloadUrl": "..."}
        {"data": {"url": "..."}}
        {"track": {"download": "..."}}
        {"audio": {"url": "..."}}
        {"downloads": [{"url": "..."}]}

    etc.
    """

    candidates: list[str] = []

    # First: values attached to explicit audio-related keys.
    values = _find_values(
        obj,
        AUDIO_URL_KEYS,
    )

    for value in values:

        if isinstance(value, str):

            if _is_http_url(value):
                candidates.append(value)

        elif isinstance(value, dict):

            nested_urls = _find_values(
                value,
                (
                    "url",
                    "href",
                    "src",
                    "downloadUrl",
                    "download_url",
                    "audioUrl",
                    "audio_url",
                    "link",
                ),
            )

            for nested in nested_urls:
                if _is_http_url(nested):
                    candidates.append(nested)

        elif isinstance(value, list):

            for item in value:

                if isinstance(item, str):
                    if _is_http_url(item):
                        candidates.append(item)

                elif isinstance(item, dict):

                    nested_urls = _find_values(
                        item,
                        (
                            "url",
                            "href",
                            "src",
                            "downloadUrl",
                            "download_url",
                            "audioUrl",
                            "audio_url",
                            "link",
                        ),
                    )

                    for nested in nested_urls:
                        if _is_http_url(nested):
                            candidates.append(nested)

    # Second: scan every URL in the response.
    if isinstance(obj, (dict, list)):
        candidates.extend(_find_all_urls(obj))

    # Deduplicate while preserving order.
    unique: list[str] = []

    for url in candidates:
        if url not in unique:
            unique.append(url)

    if not unique:
        return None

    # Highest scoring candidate wins.
    unique.sort(
        key=_score_audio_url,
        reverse=True,
    )

    # Do not accept a plain Spotify track page.
    for url in unique:

        lower = url.lower()

        if "open.spotify.com/track/" in lower:
            continue

        return url

    return None


def _find_all_urls(obj: Any) -> list[str]:
    """
    Find every HTTP/HTTPS URL anywhere inside JSON.
    """

    urls: list[str] = []

    if isinstance(obj, str):

        if _is_http_url(obj):
            urls.append(obj)

        return urls

    if isinstance(obj, dict):

        for value in obj.values():
            urls.extend(_find_all_urls(value))

    elif isinstance(obj, list):

        for item in obj:
            urls.extend(_find_all_urls(item))

    return urls


def _extract_quality(obj: Any) -> str:
    value = _first_value(
        obj,
        (
            "quality",
            "audioQuality",
            "audio_quality",
            "bitrate",
            "bitRate",
            "bit_rate",
        ),
    )

    if value is None:
        return ""

    return _string(value)


# ============================================================
# File helpers
# ============================================================


INVALID_FILENAME_CHARS = re.compile(
    r'[<>:"/\\|?*\x00-\x1F]'
)


def _safe_filename(name: str) -> str:
    """
    Make a Telegram-friendly filesystem filename.
    """

    name = _string(name)

    if not name:
        name = "spotify_audio"

    name = INVALID_FILENAME_CHARS.sub(
        "_",
        name,
    )

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    name = name.rstrip(".")

    if len(name) > MAX_FILENAME_LENGTH:
        name = name[:MAX_FILENAME_LENGTH].rstrip()

    return name or "spotify_audio"


def _extension_from_url(url: str) -> str:
    """
    Guess a file extension from a URL.
    """

    try:
        path = urlparse(url).path.lower()

        extension = Path(path).suffix

        if extension and len(extension) <= 8:
            if re.fullmatch(r"\.[a-z0-9]+", extension):
                return extension

    except Exception:
        pass

    return ""


def _extension_from_content_type(
    content_type: str,
) -> str:
    """
    Guess file extension from HTTP Content-Type.
    """

    content_type = (
        content_type or ""
    ).split(";", 1)[0].strip().lower()

    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/flac": ".flac",
        "audio/webm": ".webm",
        "video/mp4": ".mp4",
    }

    return mapping.get(content_type, "")


def _ensure_job_dir(
    output_dir: str | Path,
    track_id: str,
) -> Path:
    """
    Create an isolated temporary directory for one download.
    """

    output_path = Path(output_dir)

    output_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = int(time.time() * 1000)

    job_dir = (
        output_path
        / f"spotify_{track_id}_{timestamp}"
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return job_dir


# ============================================================
# Audio downloader
# ============================================================


def _download_file(
    url: str,
    destination: Path,
) -> str:
    """
    Download audio from the URL returned by /track.

    Returns the detected Content-Type.
    """

    temporary = destination.with_suffix(
        destination.suffix + ".part"
    )

    try:
        with session.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
            },
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            content_length = response.headers.get(
                "Content-Length",
                "",
            )

            print(
                "[Spotify] Download response:",
                response.status_code,
                content_type,
                content_length,
            )

            with open(
                temporary,
                "wb",
            ) as file:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if chunk:
                        file.write(chunk)

        if not temporary.exists():
            raise SpotifyDownloadError(
                "Spotify download produced no file."
            )

        if temporary.stat().st_size <= 0:
            raise SpotifyDownloadError(
                "Spotify download returned an empty file."
            )

        temporary.replace(destination)

        return content_type

    except requests.RequestException as exc:

        try:
            temporary.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        raise SpotifyDownloadError(
            f"Audio download failed: {exc}"
        ) from exc

    except Exception:

        try:
            temporary.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        raise


# ============================================================
# Main Spotify download function
# ============================================================


def download_spotify_song(
    track_id: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    """
    Download a Spotify track through spotify-api40 /track.

    Compatible with the current bot.py.

    Parameters
    ----------
    track_id:
        Spotify track ID or Spotify track URL.

    output_dir:
        Directory where the temporary download should be stored.

    Returns
    -------
    dict
        {
            "path": "...",
            "filename": "...",
            "title": "...",
            "artist": "...",
            "album": "...",
            "duration": ...,
            "quality": "...",
            "artwork": "...",
            "job_dir": "...",
            "source": "spotify",
            "spotify_id": "..."
        }
    """

    normalized_id = _normalize_track_id(track_id)

    if not normalized_id:
        raise SpotifyDownloadError(
            "Invalid Spotify track ID."
        )

    print(
        f"[Spotify] Downloading track: {normalized_id}"
    )

    # --------------------------------------------------------
    # Call /track
    # --------------------------------------------------------

    params = {
        "id": normalized_id,
    }

    data = _request(
        "GET",
        TRACK_ENDPOINT,
        params=params,
    )

    # --------------------------------------------------------
    # Check API success field if present
    # --------------------------------------------------------

    if isinstance(data, dict):

        success = data.get("success")

        if success is False:

            message = (
                data.get("message")
                or data.get("error")
                or "Spotify /track request failed."
            )

            raise SpotifyDownloadError(
                _string(message)
            )

    # --------------------------------------------------------
    # Extract metadata
    # --------------------------------------------------------

    title = _extract_title(data)

    artist = _extract_artist_from_object(data)

    album = _extract_album(data)

    duration = _extract_duration(data)

    artwork = _extract_artwork(data)

    quality = _extract_quality(data)

    # --------------------------------------------------------
    # Extract actual audio URL
    # --------------------------------------------------------

    audio_url = _find_audio_url(data)

    if not audio_url:

        print(
            "[Spotify] /track response did not contain "
            "a recognized audio URL."
        )

        try:
            print(
                "[Spotify] API response:"
            )
            print(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=2,
                )[:10000]
            )
        except Exception:
            print(data)

        raise SpotifyDownloadError(
            "Spotify /track response did not contain "
            "a usable audio/download URL."
        )

    print(
        "[Spotify] Audio URL found."
    )

    # --------------------------------------------------------
    # Defaults
    # --------------------------------------------------------

    if not title:
        title = f"Spotify Track {normalized_id}"

    if not artist:
        artist = "Unknown Artist"

    # --------------------------------------------------------
    # Create isolated job directory
    # --------------------------------------------------------

    job_dir = _ensure_job_dir(
        output_dir,
        normalized_id,
    )

    # --------------------------------------------------------
    # Guess extension
    # --------------------------------------------------------

    extension = _extension_from_url(
        audio_url
    )

    if not extension:
        extension = ".audio"

    base_name = _safe_filename(
        f"{artist} - {title}"
    )

    output_path = (
        job_dir
        / f"{base_name}{extension}"
    )

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    content_type = _download_file(
        audio_url,
        output_path,
    )

    # --------------------------------------------------------
    # If URL didn't expose extension, use Content-Type
    # --------------------------------------------------------

    if extension == ".audio":

        content_extension = (
            _extension_from_content_type(
                content_type
            )
        )

        if content_extension:

            corrected_path = (
                job_dir
                / f"{base_name}{content_extension}"
            )

            try:
                output_path.replace(
                    corrected_path
                )
                output_path = corrected_path
            except Exception:
                pass

    # --------------------------------------------------------
    # Final validation
    # --------------------------------------------------------

    if not output_path.exists():
        raise SpotifyDownloadError(
            "Spotify audio file was not created."
        )

    if output_path.stat().st_size <= 0:
        raise SpotifyDownloadError(
            "Spotify audio file is empty."
        )

    print(
        "[Spotify] Download complete:",
        output_path,
        output_path.stat().st_size,
        "bytes",
    )

    # --------------------------------------------------------
    # Return structure expected by bot.py
    # --------------------------------------------------------

    return {
        "path": str(output_path),
        "filename": output_path.name,
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "quality": quality,
        "artwork": artwork,
        "job_dir": str(job_dir),
        "source": "spotify",
        "spotify_id": normalized_id,
    }


# ============================================================
# Cleanup helper
# ============================================================


def cleanup_spotify_job(
    result: dict[str, Any] | None,
) -> None:
    """
    Remove the temporary Spotify job directory.

    This is optional because bot.py already performs cleanup,
    but keeping this helper makes spotify.py independently
    usable.
    """

    if not result:
        return

    job_dir = result.get("job_dir")

    if not job_dir:
        return

    try:
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        print(
            "[Spotify] Cleaned up:",
            job_dir,
        )

    except Exception as exc:
        print(
            "[Spotify] Cleanup warning:",
            exc,
        )


# ============================================================
# Module diagnostics
# ============================================================


def spotify_configured() -> bool:
    """
    Return whether the new RapidAPI credentials are configured.
    """

    return bool(
        SPOTIFY_API40_KEY
        and SPOTIFY_API40_HOST
    )


if __name__ == "__main__":
    print("Spotify API configuration")
    print(
        "Host:",
        SPOTIFY_API40_HOST,
    )
    print(
        "Key configured:",
        bool(SPOTIFY_API40_KEY),
    )