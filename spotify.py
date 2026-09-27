"""
Audio-Bot Spotify integration.

API 1:
    spotify-api40.p.rapidapi.com
    Used for Spotify search / metadata.

API 2:
    Spotify Downloader RapidAPI
    Used ONLY for downloading the actual full-length audio.

Required environment variables:

    SPOTIFY_API40_KEY
    SPOTIFY_API40_HOST

    SPOTIFY_RAPIDAPI_KEY
    SPOTIFY_RAPIDAPI_HOST

Current bot.py compatibility:

    search_spotify(query, limit=8)

    download_spotify_song(track_id, output_dir)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


# ============================================================
# CONFIGURATION
# ============================================================

# ------------------------------------------------------------
# API 1: spotify-api40
# ------------------------------------------------------------

SPOTIFY_API40_KEY = os.getenv(
    "SPOTIFY_API40_KEY",
    "",
).strip()

SPOTIFY_API40_HOST = os.getenv(
    "SPOTIFY_API40_HOST",
    "spotify-api40.p.rapidapi.com",
).strip()

API40_BASE_URL = (
    f"https://{SPOTIFY_API40_HOST}"
)

API40_SEARCH_URL = (
    f"{API40_BASE_URL}/search"
)


# ------------------------------------------------------------
# API 2: Spotify Downloader
# ------------------------------------------------------------

SPOTIFY_RAPIDAPI_KEY = os.getenv(
    "SPOTIFY_RAPIDAPI_KEY",
    "",
).strip()

SPOTIFY_RAPIDAPI_HOST = os.getenv(
    "SPOTIFY_RAPIDAPI_HOST",
    "latest-spotify-downloader.p.rapidapi.com",
).strip()

DOWNLOADER_BASE_URL = (
    f"https://{SPOTIFY_RAPIDAPI_HOST}"
)

DOWNLOADER_URL = (
    f"{DOWNLOADER_BASE_URL}/downloadSong"
)


# ------------------------------------------------------------
# Timeouts
# ------------------------------------------------------------

REQUEST_TIMEOUT = int(
    os.getenv(
        "SPOTIFY_API40_TIMEOUT",
        "60",
    )
)

DOWNLOAD_TIMEOUT = int(
    os.getenv(
        "SPOTIFY_DOWNLOAD_TIMEOUT",
        "300",
    )
)


USER_AGENT = "Audio-Bot/3.1"

MAX_FILENAME_LENGTH = 180


# ============================================================
# EXCEPTIONS
# ============================================================


class SpotifyError(Exception):
    """Base Spotify error."""


class SpotifyAPIError(SpotifyError):
    """Spotify API error."""


class SpotifyDownloadError(SpotifyError):
    """Spotify audio download error."""


# ============================================================
# HTTP SESSION
# ============================================================


session = requests.Session()

session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
)


# ============================================================
# GENERAL HELPERS
# ============================================================


def _text(value: Any) -> str:

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    return str(value).strip()


def _is_http_url(
    value: Any,
) -> bool:

    if not isinstance(value, str):
        return False

    try:

        parsed = urlparse(value)

        return (
            parsed.scheme
            in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:

        return False


def _safe_json(
    response: requests.Response,
) -> Any:

    try:

        return response.json()

    except Exception:

        return None


def _request_api40(
    params: dict[str, Any],
) -> Any:

    if not SPOTIFY_API40_KEY:

        raise SpotifyAPIError(
            "SPOTIFY_API40_KEY is not configured."
        )

    headers = {
        "x-rapidapi-key":
            SPOTIFY_API40_KEY,

        "x-rapidapi-host":
            SPOTIFY_API40_HOST,

        "Accept":
            "application/json",

        "User-Agent":
            USER_AGENT,
    }

    try:

        response = session.get(
            API40_SEARCH_URL,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    except requests.RequestException as exc:

        raise SpotifyAPIError(
            f"Spotify search request failed: {exc}"
        ) from exc

    data = _safe_json(response)

    if response.status_code >= 400:

        message = ""

        if isinstance(data, dict):

            message = (
                data.get("message")
                or data.get("error")
                or data.get("detail")
                or ""
            )

        if not message:

            message = response.text[:500]

        raise SpotifyAPIError(
            f"Spotify API returned HTTP "
            f"{response.status_code}: {message}"
        )

    if data is None:

        raise SpotifyAPIError(
            "Spotify API returned invalid JSON."
        )

    return data


def _request_downloader(
    track_id: str,
) -> Any:

    if not SPOTIFY_RAPIDAPI_KEY:

        raise SpotifyDownloadError(
            "SPOTIFY_RAPIDAPI_KEY is not configured."
        )

    headers = {
        "x-rapidapi-key":
            SPOTIFY_RAPIDAPI_KEY,

        "x-rapidapi-host":
            SPOTIFY_RAPIDAPI_HOST,

        "Accept":
            "application/json",

        "User-Agent":
            USER_AGENT,
    }

    try:

        response = session.get(
            DOWNLOADER_URL,
            headers=headers,
            params={
                "songId": track_id,
            },
            timeout=REQUEST_TIMEOUT,
        )

    except requests.RequestException as exc:

        raise SpotifyDownloadError(
            f"Spotify downloader request failed: {exc}"
        ) from exc

    data = _safe_json(response)

    if response.status_code >= 400:

        message = ""

        if isinstance(data, dict):

            message = (
                data.get("message")
                or data.get("error")
                or data.get("detail")
                or ""
            )

        if not message:

            message = response.text[:500]

        raise SpotifyDownloadError(
            f"Spotify downloader returned HTTP "
            f"{response.status_code}: {message}"
        )

    if data is None:

        raise SpotifyDownloadError(
            "Spotify downloader returned invalid JSON."
        )

    return data


# ============================================================
# RECURSIVE JSON HELPERS
# ============================================================


def _find_key_values(
    obj: Any,
    keys: set[str],
) -> list[Any]:

    results = []

    if isinstance(obj, dict):

        for key, value in obj.items():

            if key in keys:

                results.append(value)

            results.extend(
                _find_key_values(
                    value,
                    keys,
                )
            )

    elif isinstance(obj, list):

        for item in obj:

            results.extend(
                _find_key_values(
                    item,
                    keys,
                )
            )

    return results


def _find_first(
    obj: Any,
    keys: tuple[str, ...],
) -> Any:

    values = _find_key_values(
        obj,
        set(keys),
    )

    for value in values:

        if value not in (
            None,
            "",
            [],
            {},
        ):

            return value

    return None


def _find_all_urls(
    obj: Any,
) -> list[str]:

    urls = []

    if isinstance(obj, str):

        if _is_http_url(obj):

            urls.append(obj)

        return urls

    if isinstance(obj, dict):

        for value in obj.values():

            urls.extend(
                _find_all_urls(value)
            )

    elif isinstance(obj, list):

        for item in obj:

            urls.extend(
                _find_all_urls(item)
            )

    return urls


# ============================================================
# SPOTIFY ID HELPERS
# ============================================================


def extract_spotify_track_id(
    value: str,
) -> str | None:

    if not value:
        return None

    value = value.strip()

    # spotify:track:ID
    match = re.search(
        r"spotify:track:([A-Za-z0-9]+)",
        value,
        re.IGNORECASE,
    )

    if match:

        return match.group(1)


    # https://open.spotify.com/track/ID
    match = re.search(
        r"open\.spotify\.com/track/([A-Za-z0-9]+)",
        value,
        re.IGNORECASE,
    )

    if match:

        return match.group(1)

    return None


def _normalize_track_id(
    value: Any,
) -> str:

    value = _text(value)

    if not value:
        return ""

    track_id = extract_spotify_track_id(
        value
    )

    if track_id:

        return track_id


    # Plain Spotify ID
    if re.fullmatch(
        r"[A-Za-z0-9]{10,64}",
        value,
    ):

        return value

    return ""


# ============================================================
# TRACK DETECTION
# ============================================================


def _is_track_object(
    obj: Any,
) -> bool:

    if not isinstance(obj, dict):

        return False

    typename = _text(
        obj.get("__typename")
    ).lower()

    object_type = _text(
        obj.get("type")
    ).lower()

    uri = _text(
        obj.get("uri")
    ).lower()

    # API40 / Spotify style.
    if typename == "track":

        return True

    if object_type == "track":

        return True

    if uri.startswith(
        "spotify:track:"
    ):

        return True

    # IMPORTANT:
    #
    # Do NOT simply check for "id".
    #
    # Albums also contain IDs.
    #
    return False


def _find_track_objects(
    obj: Any,
) -> list[dict[str, Any]]:

    tracks = []

    if isinstance(obj, dict):

        if _is_track_object(obj):

            tracks.append(obj)

        for value in obj.values():

            tracks.extend(
                _find_track_objects(
                    value
                )
            )

    elif isinstance(obj, list):

        for item in obj:

            tracks.extend(
                _find_track_objects(
                    item
                )
            )

    return tracks


# ============================================================
# TRACK METADATA
# ============================================================


def _extract_track_id(
    track: dict[str, Any],
) -> str:

    # Prefer Spotify URI.
    uri = _text(
        track.get("uri")
    )

    if uri.startswith(
        "spotify:track:"
    ):

        return _normalize_track_id(
            uri
        )


    for key in (
        "id",
        "trackId",
        "track_id",
    ):

        track_id = _normalize_track_id(
            track.get(key)
        )

        if track_id:

            return track_id

    return ""


def _extract_title(
    track: dict[str, Any],
) -> str:

    for key in (
        "name",
        "title",
        "trackName",
        "track_name",
    ):

        value = track.get(key)

        if isinstance(
            value,
            str,
        ) and value.strip():

            return value.strip()

    return "Unknown Track"


def _extract_artist(
    track: dict[str, Any],
) -> str:

    names = []

    artists = track.get(
        "artists"
    )

    if isinstance(
        artists,
        list,
    ):

        for artist in artists:

            if not isinstance(
                artist,
                dict,
            ):
                continue

            name = artist.get(
                "name"
            )

            if name:

                names.append(
                    _text(name)
                )

                continue

            profile = artist.get(
                "profile"
            )

            if isinstance(
                profile,
                dict,
            ):

                name = profile.get(
                    "name"
                )

                if name:

                    names.append(
                        _text(name)
                    )


    elif isinstance(
        artists,
        dict,
    ):

        name = artists.get(
            "name"
        )

        if name:

            names.append(
                _text(name)
            )

        profile = artists.get(
            "profile"
        )

        if isinstance(
            profile,
            dict,
        ):

            name = profile.get(
                "name"
            )

            if name:

                names.append(
                    _text(name)
                )


    # Remove duplicates.
    unique = []

    for name in names:

        if name and name not in unique:

            unique.append(name)

    return (
        ", ".join(unique)
        or "Unknown Artist"
    )


def _extract_album(
    track: dict[str, Any],
) -> str:

    album = track.get(
        "album"
    )

    if isinstance(
        album,
        str,
    ):

        return album

    if isinstance(
        album,
        dict,
    ):

        return _text(
            album.get("name")
            or album.get("title")
            or ""
        )

    return ""


def _extract_duration(
    track: dict[str, Any],
) -> int | None:

    value = (
        track.get("duration_ms")
        or track.get("durationMs")
        or track.get("duration")
    )

    if value is None:

        return None

    try:

        value = float(value)

        # Milliseconds → seconds.
        if value > 10000:

            value /= 1000

        return int(value)

    except (
        TypeError,
        ValueError,
    ):

        return None


def _extract_artwork(
    track: dict[str, Any],
) -> str | None:

    # Standard Spotify images.
    album = track.get(
        "album"
    )

    if isinstance(
        album,
        dict,
    ):

        images = album.get(
            "images"
        )

        if isinstance(
            images,
            list,
        ):

            for image in images:

                if not isinstance(
                    image,
                    dict,
                ):
                    continue

                url = image.get(
                    "url"
                )

                if _is_http_url(url):

                    return url


    # API40 structure:
    #
    # coverArt
    #   sources
    #      height
    #      url
    #      width
    #
    cover_art = track.get(
        "coverArt"
    )

    if isinstance(
        cover_art,
        dict,
    ):

        sources = cover_art.get(
            "sources"
        )

        if isinstance(
            sources,
            list,
        ):

            candidates = []

            for source in sources:

                if not isinstance(
                    source,
                    dict,
                ):
                    continue

                url = source.get(
                    "url"
                )

                if not _is_http_url(url):
                    continue

                try:

                    width = int(
                        source.get(
                            "width",
                            0,
                        )
                    )

                except Exception:

                    width = 0

                candidates.append(
                    (
                        width,
                        url,
                    )
                )

            if candidates:

                candidates.sort(
                    reverse=True
                )

                return candidates[0][1]

    return None


def _normalize_track(
    track: dict[str, Any],
) -> dict[str, Any]:

    track_id = _extract_track_id(
        track
    )

    title = _extract_title(
        track
    )

    artist = _extract_artist(
        track
    )

    album = _extract_album(
        track
    )

    duration = _extract_duration(
        track
    )

    artwork = _extract_artwork(
        track
    )

    spotify_url = ""

    uri = _text(
        track.get("uri")
    )

    if uri.startswith(
        "spotify:track:"
    ):

        spotify_url = (
            "https://open.spotify.com/track/"
            + track_id
        )

    return {
        "id": track_id,
        "track_id": track_id,
        "spotify_id": track_id,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artist,
        "album": album,
        "duration": duration,
        "artwork": artwork,
        "image": artwork,
        "url": spotify_url,
        "raw": track,
    }


# ============================================================
# SEARCH
# ============================================================


def search_spotify(
    query: str,
    limit: int = 8,
) -> dict[str, Any]:

    query = _text(query)

    if not query:

        return {
            "query": "",
            "results": [],
            "raw": {},
        }

    limit = max(
        1,
        min(
            int(limit),
            20,
        ),
    )

    print(
        f"[Spotify] Searching: {query}"
    )

    data = _request_api40(
        {
            "query": query,
        }
    )

    # --------------------------------------------------------
    # ONLY TRACK OBJECTS
    # --------------------------------------------------------

    track_objects = _find_track_objects(
        data
    )

    results = []

    seen_ids = set()

    for track in track_objects:

        normalized = _normalize_track(
            track
        )

        track_id = normalized.get(
            "id"
        )

        if not track_id:
            continue

        if track_id in seen_ids:
            continue

        seen_ids.add(
            track_id
        )

        results.append(
            normalized
        )

        if len(results) >= limit:

            break

    print(
        f"[Spotify] Found "
        f"{len(results)} track(s)"
    )

    if not results:

        print(
            "[Spotify] WARNING: "
            "No Track objects were found."
        )

        try:

            print(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=2,
                )[:10000]
            )

        except Exception:

            pass

    return {
        "query": query,
        "results": results,
        "raw": data,
    }


# ============================================================
# OLD DOWNLOADER RESPONSE PARSER
# ============================================================


DOWNLOAD_URL_KEYS = (
    "url",
    "downloadUrl",
    "download_url",
    "download",
    "audioUrl",
    "audio_url",
    "audio",
    "audioDownloadUrl",
    "audio_download_url",
    "fileUrl",
    "file_url",
    "file",
    "mediaUrl",
    "media_url",
    "streamUrl",
    "stream_url",
    "stream",
    "link",
    "href",
)


def _score_download_url(
    url: str,
) -> int:

    lower = url.lower()

    score = 0

    for token in (
        ".mp3",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wav",
        ".flac",
        "download",
        "audio",
        "media",
        "cdn",
        "stream",
    ):

        if token in lower:

            score += 10

    # Never select Spotify page/image URLs.
    if (
        "open.spotify.com"
        in lower
    ):

        score -= 100

    if (
        "i.scdn.co"
        in lower
    ):

        score -= 100

    return score


def _find_download_url(
    data: Any,
) -> str | None:

    candidates = []

    # Explicit download-related keys.
    values = _find_key_values(
        data,
        set(DOWNLOAD_URL_KEYS),
    )

    for value in values:

        if isinstance(
            value,
            str,
        ):

            if _is_http_url(value):

                candidates.append(
                    value
                )

        elif isinstance(
            value,
            dict,
        ):

            candidates.extend(
                _find_all_urls(value)
            )

        elif isinstance(
            value,
            list,
        ):

            candidates.extend(
                _find_all_urls(value)
            )

    # Fallback: all URLs in response.
    candidates.extend(
        _find_all_urls(data)
    )

    # Remove duplicates.
    unique = []

    for url in candidates:

        if url not in unique:

            unique.append(url)

    if not unique:

        return None

    unique.sort(
        key=_score_download_url,
        reverse=True,
    )

    for url in unique:

        if _score_download_url(url) > -50:

            return url

    return None


# ============================================================
# DOWNLOAD FILE
# ============================================================


def _safe_filename(
    value: str,
) -> str:

    value = _text(value)

    if not value:

        value = "spotify_audio"

    value = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    value = value.rstrip(
        "."
    )

    if len(value) > MAX_FILENAME_LENGTH:

        value = (
            value[
                :MAX_FILENAME_LENGTH
            ]
            .rstrip()
        )

    return (
        value
        or "spotify_audio"
    )


def _extension_from_url(
    url: str,
) -> str:

    try:

        extension = Path(
            urlparse(url).path
        ).suffix.lower()

        if re.fullmatch(
            r"\.[a-z0-9]{1,8}",
            extension,
        ):

            return extension

    except Exception:

        pass

    return ""


def _extension_from_content_type(
    content_type: str,
) -> str:

    content_type = (
        content_type
        .split(";", 1)[0]
        .strip()
        .lower()
    )

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

    return mapping.get(
        content_type,
        "",
    )


def _download_file(
    url: str,
    destination: Path,
) -> str:

    temporary = destination.with_name(
        destination.name + ".part"
    )

    try:

        with session.get(
            url,
            headers={
                "User-Agent":
                    USER_AGENT,
                "Accept":
                    "*/*",
            },
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            content_type = (
                response.headers.get(
                    "Content-Type",
                    "",
                )
            )

            print(
                "[Spotify] Audio HTTP:",
                response.status_code,
            )

            print(
                "[Spotify] Content-Type:",
                content_type,
            )

            with open(
                temporary,
                "wb",
            ) as file:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if chunk:

                        file.write(
                            chunk
                        )

        if not temporary.exists():

            raise SpotifyDownloadError(
                "Spotify audio file was not created."
            )

        if (
            temporary.stat().st_size
            <= 0
        ):

            raise SpotifyDownloadError(
                "Spotify audio file is empty."
            )

        temporary.replace(
            destination
        )

        return content_type

    except requests.RequestException as exc:

        try:

            temporary.unlink(
                missing_ok=True
            )

        except Exception:

            pass

        raise SpotifyDownloadError(
            f"Spotify audio download failed: {exc}"
        ) from exc


# ============================================================
# MAIN DOWNLOAD FUNCTION
# ============================================================


def download_spotify_song(
    track_id: str,
    output_dir: str | Path,
) -> dict[str, Any]:

    normalized_id = (
        _normalize_track_id(
            track_id
        )
    )

    if not normalized_id:

        raise SpotifyDownloadError(
            "Invalid Spotify track ID."
        )

    print(
        "[Spotify] Full-track downloader:"
        f" {normalized_id}"
    )

    # ========================================================
    # IMPORTANT:
    #
    # We DO NOT use API40 /track for audio.
    #
    # The old downloader API is responsible for the
    # actual full-length audio.
    # ========================================================

    data = _request_downloader(
        normalized_id
    )

    # --------------------------------------------------------
    # Find audio URL
    # --------------------------------------------------------

    audio_url = _find_download_url(
        data
    )

    if not audio_url:

        print(
            "[Spotify] Downloader returned "
            "no usable audio URL."
        )

        try:

            print(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=2,
                )[:12000]
            )

        except Exception:

            print(data)

        raise SpotifyDownloadError(
            "Spotify downloader returned "
            "no usable audio URL."
        )

    print(
        "[Spotify] Full audio URL received."
    )

    # --------------------------------------------------------
    # Metadata
    # --------------------------------------------------------

    title = _text(
        _find_first(
            data,
            (
                "title",
                "name",
                "trackName",
                "track_name",
                "songName",
                "song_name",
            ),
        )
    )

    artist = _text(
        _find_first(
            data,
            (
                "artist",
                "artistName",
                "artist_name",
                "performer",
                "author",
            ),
        )
    )

    album = _text(
        _find_first(
            data,
            (
                "album",
                "albumName",
                "album_name",
            ),
        )
    )

    duration = _find_first(
        data,
        (
            "duration",
            "duration_ms",
            "durationMs",
        ),
    )

    try:

        if duration is not None:

            duration = float(
                duration
            )

            if duration > 10000:

                duration /= 1000

            duration = int(
                duration
            )

    except Exception:

        duration = None

    quality = _text(
        _find_first(
            data,
            (
                "quality",
                "audioQuality",
                "audio_quality",
                "bitrate",
                "bitRate",
                "bit_rate",
            ),
        )
    )

    if not title:

        title = (
            f"Spotify Track "
            f"{normalized_id}"
        )

    if not artist:

        artist = "Unknown Artist"

    # --------------------------------------------------------
    # Job directory
    # --------------------------------------------------------

    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    job_dir = (
        output_dir
        / (
            f"spotify_"
            f"{normalized_id}_"
            f"{int(time.time() * 1000)}"
        )
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Filename
    # --------------------------------------------------------

    base_name = _safe_filename(
        f"{artist} - {title}"
    )

    extension = _extension_from_url(
        audio_url
    )

    if not extension:

        extension = ".audio"

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
    # Correct extension
    # --------------------------------------------------------

    if output_path.suffix == ".audio":

        extension = (
            _extension_from_content_type(
                content_type
            )
        )

        if extension:

            corrected_path = (
                job_dir
                / f"{base_name}{extension}"
            )

            output_path.replace(
                corrected_path
            )

            output_path = (
                corrected_path
            )

    # --------------------------------------------------------
    # Validate
    # --------------------------------------------------------

    if not output_path.exists():

        raise SpotifyDownloadError(
            "Spotify audio file was not created."
        )

    file_size = (
        output_path.stat().st_size
    )

    if file_size <= 0:

        raise SpotifyDownloadError(
            "Spotify audio file is empty."
        )

    print(
        "[Spotify] Full audio download complete:"
    )

    print(
        "[Spotify] File:",
        output_path,
    )

    print(
        "[Spotify] Size:",
        file_size,
        "bytes",
    )

    return {
        "path": str(
            output_path
        ),

        "filename":
            output_path.name,

        "title":
            title,

        "artist":
            artist,

        "album":
            album,

        "duration":
            duration,

        "quality":
            quality,

        "artwork":
            None,

        "job_dir":
            str(job_dir),

        "source":
            "spotify",

        "spotify_id":
            normalized_id,
    }


# ============================================================
# CLEANUP
# ============================================================


def cleanup_spotify_job(
    result: dict[str, Any] | None,
) -> None:

    if not result:

        return

    job_dir = result.get(
        "job_dir"
    )

    if not job_dir:

        return

    try:

        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        print(
            "[Spotify] Cleaned:",
            job_dir,
        )

    except Exception as exc:

        print(
            "[Spotify] Cleanup warning:",
            exc,
        )


# ============================================================
# CONFIGURATION CHECK
# ============================================================


def spotify_configured() -> bool:

    return bool(
        SPOTIFY_API40_KEY
        and SPOTIFY_RAPIDAPI_KEY
    )


# ============================================================
# LOCAL DIAGNOSTIC
# ============================================================


if __name__ == "__main__":

    print(
        "========================================"
    )

    print(
        "Audio Bot Spotify Configuration"
    )

    print(
        "API40 Host:",
        SPOTIFY_API40_HOST,
    )

    print(
        "API40 Key:",
        "configured"
        if SPOTIFY_API40_KEY
        else "MISSING",
    )

    print(
        "Downloader Host:",
        SPOTIFY_RAPIDAPI_HOST,
    )

    print(
        "Downloader Key:",
        "configured"
        if SPOTIFY_RAPIDAPI_KEY
        else "MISSING",
    )

    print(
        "========================================"
    )