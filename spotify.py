import os
import re
import json
import time
import shutil
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import requests


# ============================================================
# CONFIGURATION
# ============================================================

SPOTIFY_API_KEY = os.getenv(
    "SPOTIFY_RAPIDAPI_KEY",
    "",
).strip()


SPOTIFY_API_HOST = os.getenv(
    "SPOTIFY_RAPIDAPI_HOST",
    "latest-spotify-downloader.p.rapidapi.com",
).strip()


SPOTIFY_API_BASE = (
    f"https://{SPOTIFY_API_HOST}"
)


SPOTIFY_SEARCH_ENDPOINT = (
    "/search"
)


SPOTIFY_DOWNLOAD_ENDPOINT = (
    "/downloadSong"
)


SPOTIFY_TIMEOUT = int(
    os.getenv(
        "SPOTIFY_API_TIMEOUT",
        "60",
    )
)


SPOTIFY_DOWNLOAD_TIMEOUT = int(
    os.getenv(
        "SPOTIFY_DOWNLOAD_TIMEOUT",
        "300",
    )
)


# ============================================================
# ERRORS
# ============================================================

class SpotifyError(Exception):
    """Base Spotify API error."""


class SpotifyAPIError(SpotifyError):
    """Spotify RapidAPI request failed."""


class SpotifyDownloadError(SpotifyError):
    """Spotify audio download failed."""


# ============================================================
# HTTP SESSION
# ============================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": (
            "Audio-Bot/2.5"
        ),
        "Accept": "application/json",
    }
)


def _rapidapi_headers():
    """
    Build RapidAPI headers.
    """

    if not SPOTIFY_API_KEY:

        raise SpotifyAPIError(
            "SPOTIFY_RAPIDAPI_KEY is not configured."
        )

    return {
        "x-rapidapi-key": SPOTIFY_API_KEY,
        "x-rapidapi-host": SPOTIFY_API_HOST,
        "Accept": "application/json",
    }


# ============================================================
# GENERAL HELPERS
# ============================================================

def _safe_json(response):
    """
    Safely decode a JSON response.
    """

    try:

        return response.json()

    except ValueError:

        text = (
            response.text
            if response.text
            else ""
        )

        return {
            "_raw_text": text
        }


def _check_api_response(
    response,
    operation="Spotify API request",
):
    """
    Validate HTTP response and Spotify API's
    success flag.
    """

    if response.status_code >= 400:

        data = _safe_json(response)

        message = (
            data.get("message")
            if isinstance(data, dict)
            else None
        )

        raise SpotifyAPIError(
            f"{operation} failed "
            f"(HTTP {response.status_code})"
            + (
                f": {message}"
                if message
                else ""
            )
        )

    data = _safe_json(response)

    if isinstance(data, dict):

        success = data.get(
            "success"
        )

        if success is False:

            message = (
                data.get("message")
                or "Unknown Spotify API error."
            )

            raise SpotifyAPIError(
                str(message)
            )

    return data


# ============================================================
# SPOTIFY URL HELPERS
# ============================================================

SPOTIFY_URL_REGEX = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?"
    r"(track|album|playlist|artist)/"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def extract_spotify_resource(
    value: str,
):
    """
    Extract Spotify resource type and ID.

    Example:
        https://open.spotify.com/track/ABC123

    Returns:
        ("track", "ABC123")
    """

    if not value:
        return None

    match = SPOTIFY_URL_REGEX.search(
        value.strip()
    )

    if not match:
        return None

    return (
        match.group(1).lower(),
        match.group(2),
    )


def extract_spotify_track_id(
    value: str,
):
    """
    Extract only a Spotify track ID.

    Accepts:

        Spotify URL
        spotify:track:ID
        raw track ID
    """

    if not value:
        return None

    value = str(value).strip()

    # --------------------------------------------------------
    # Spotify URL
    # --------------------------------------------------------

    parsed = extract_spotify_resource(
        value
    )

    if parsed:

        resource_type, resource_id = (
            parsed
        )

        if resource_type == "track":
            return resource_id

        return None

    # --------------------------------------------------------
    # Spotify URI
    # --------------------------------------------------------

    uri_match = re.match(
        r"^spotify:track:([A-Za-z0-9]+)$",
        value,
        re.IGNORECASE,
    )

    if uri_match:

        return uri_match.group(1)

    # --------------------------------------------------------
    # Raw Spotify ID
    # --------------------------------------------------------

    if re.fullmatch(
        r"[A-Za-z0-9]{10,40}",
        value,
    ):

        return value

    return None


# ============================================================
# NORMALIZE SEARCH RESULTS
# ============================================================

def _first_value(
    data,
    keys,
    default=None,
):
    """
    Return the first useful value from a dictionary.
    """

    if not isinstance(data, dict):
        return default

    for key in keys:

        value = data.get(key)

        if value is not None:

            if isinstance(
                value,
                str,
            ):

                if value.strip():
                    return value.strip()

            else:

                return value

    return default


def _extract_artist_name(
    track,
):
    """
    Handle different artist representations.
    """

    if not isinstance(track, dict):
        return "Unknown Artist"

    artist = _first_value(
        track,
        [
            "artist",
            "artistName",
            "artists",
            "performer",
            "author",
        ],
    )

    if isinstance(
        artist,
        str,
    ):

        return artist.strip()

    if isinstance(
        artist,
        list,
    ):

        names = []

        for item in artist:

            if isinstance(
                item,
                str,
            ):

                names.append(
                    item
                )

            elif isinstance(
                item,
                dict,
            ):

                name = _first_value(
                    item,
                    [
                        "name",
                        "artist",
                        "artistName",
                    ],
                )

                if name:
                    names.append(
                        str(name)
                    )

        if names:

            return ", ".join(
                names
            )

    if isinstance(
        artist,
        dict,
    ):

        name = _first_value(
            artist,
            [
                "name",
                "artist",
                "artistName",
            ],
        )

        if name:
            return str(name)

    return "Unknown Artist"


def _extract_track_id(
    track,
):
    """
    Extract Spotify track ID from a search result.
    """

    if not isinstance(track, dict):
        return None

    # Most likely fields first.
    direct_id = _first_value(
        track,
        [
            "id",
            "track_id",
            "trackId",
            "spotify_id",
            "spotifyId",
        ],
    )

    if direct_id:

        direct_id = str(
            direct_id
        ).strip()

        # If the API returns a Spotify URL
        # inside the ID field.
        extracted = extract_spotify_track_id(
            direct_id
        )

        if extracted:
            return extracted

        if re.fullmatch(
            r"[A-Za-z0-9]{10,40}",
            direct_id,
        ):

            return direct_id

    # Search common URL fields.
    for key in (
        "url",
        "uri",
        "spotify_url",
        "spotifyUrl",
        "external_url",
        "externalUrl",
        "link",
    ):

        value = track.get(key)

        if not value:
            continue

        extracted = extract_spotify_track_id(
            str(value)
        )

        if extracted:
            return extracted

    return None


def _normalize_track(
    track,
):
    """
    Convert an arbitrary Spotify search result
    into the structure expected by bot.py.
    """

    if not isinstance(
        track,
        dict,
    ):

        return None

    track_id = _extract_track_id(
        track
    )

    title = _first_value(
        track,
        [
            "title",
            "name",
            "track_name",
            "trackName",
        ],
        "Unknown Track",
    )

    artist = _extract_artist_name(
        track
    )

    album = _first_value(
        track,
        [
            "album",
            "albumName",
        ],
        "",
    )

    if isinstance(
        album,
        dict,
    ):

        album = _first_value(
            album,
            [
                "name",
                "title",
            ],
            "",
        )

    duration = _first_value(
        track,
        [
            "duration",
            "duration_ms",
            "durationMs",
        ],
    )

    # Convert milliseconds to seconds.
    if duration:

        try:

            duration = float(
                duration
            )

            if duration > 10000:
                duration = duration / 1000

        except (
            TypeError,
            ValueError,
        ):

            duration = None

    artwork = _first_value(
        track,
        [
            "image",
            "imageUrl",
            "image_url",
            "cover",
            "coverUrl",
            "cover_url",
            "thumbnail",
            "thumbnailUrl",
        ],
        "",
    )

    # Sometimes artwork is nested.
    if isinstance(
        artwork,
        dict,
    ):

        artwork = _first_value(
            artwork,
            [
                "url",
                "src",
            ],
            "",
        )

    if not artwork:

        album_data = track.get(
            "album"
        )

        if isinstance(
            album_data,
            dict,
        ):

            artwork = _first_value(
                album_data,
                [
                    "image",
                    "imageUrl",
                    "image_url",
                    "cover",
                    "coverUrl",
                    "cover_url",
                    "thumbnail",
                ],
                "",
            )

    return {
        "id": track_id,
        "title": str(
            title
            or "Unknown Track"
        ),
        "artist": str(
            artist
            or "Unknown Artist"
        ),
        "album": str(
            album
            or ""
        ),
        "duration": duration,
        "artwork": str(
            artwork
            or ""
        ),
        "raw": track,
    }


# ============================================================
# SEARCH RESULT EXTRACTION
# ============================================================

def _find_track_list(
    data,
):
    """
    Locate the track list in different possible
    Spotify API response structures.
    """

    if not data:
        return []

    # --------------------------------------------------------
    # Direct list
    # --------------------------------------------------------

    if isinstance(
        data,
        list,
    ):

        return data

    if not isinstance(
        data,
        dict,
    ):

        return []

    # --------------------------------------------------------
    # Common direct keys
    # --------------------------------------------------------

    for key in (
        "tracks",
        "items",
        "results",
        "data",
    ):

        value = data.get(
            key
        )

        if isinstance(
            value,
            list,
        ):

            return value

        if isinstance(
            value,
            dict,
        ):

            nested = _find_track_list(
                value
            )

            if nested:
                return nested

    # --------------------------------------------------------
    # Search categories
    # --------------------------------------------------------

    for key in (
        "track",
        "trackResults",
        "track_results",
        "songs",
    ):

        value = data.get(
            key
        )

        if isinstance(
            value,
            list,
        ):

            return value

        if isinstance(
            value,
            dict,
        ):

            nested = _find_track_list(
                value
            )

            if nested:
                return nested

    return []


# ============================================================
# SEARCH SPOTIFY
# ============================================================

def search_spotify(
    query: str,
    limit: int = 8,
):
    """
    Search Spotify using the RapidAPI provider.

    Returns:

        {
            "query": "...",
            "results": [...]
        }
    """

    if not query:
        raise SpotifyAPIError(
            "Spotify search query is empty."
        )

    query = str(
        query
    ).strip()

    limit = max(
        1,
        min(
            10,
            int(limit),
        ),
    )

    params = {
        "q": query,
        "type": "track",
        "offset": 0,
        "limit": limit,
        "noOfTopResult": limit,
    }

    url = (
        SPOTIFY_API_BASE
        + SPOTIFY_SEARCH_ENDPOINT
    )

    print(
        "Spotify search:",
        query,
    )

    try:

        response = SESSION.get(
            url,
            headers=_rapidapi_headers(),
            params=params,
            timeout=SPOTIFY_TIMEOUT,
        )

    except requests.RequestException as error:

        raise SpotifyAPIError(
            f"Spotify search request failed: "
            f"{error}"
        ) from error

    data = _check_api_response(
        response,
        operation="Spotify search",
    )

    raw_tracks = _find_track_list(
        data
    )

    results = []

    for raw_track in raw_tracks:

        normalized = _normalize_track(
            raw_track
        )

        if not normalized:
            continue

        # A result without an ID cannot be
        # downloaded directly.
        if not normalized.get(
            "id"
        ):

            print(
                "Spotify result has no track ID:",
                raw_track,
            )

            continue

        results.append(
            normalized
        )

        if len(results) >= limit:
            break

    print(
        "Spotify results:",
        len(results),
    )

    return {
        "query": query,
        "results": results,
        "raw": data,
    }


# ============================================================
# DOWNLOAD RESPONSE URL EXTRACTION
# ============================================================

URL_KEYS = (
    "url",
    "downloadUrl",
    "download_url",
    "audioUrl",
    "audio_url",
    "streamUrl",
    "stream_url",
    "fileUrl",
    "file_url",
    "link",
    "href",
)


def _is_http_url(
    value,
):
    if not isinstance(
        value,
        str,
    ):
        return False

    value = value.strip()

    return (
        value.startswith(
            "http://"
        )
        or value.startswith(
            "https://"
        )
    )


def _find_download_url(
    data,
):
    """
    Recursively search a Spotify download response
    for a usable HTTP(S) audio URL.

    This intentionally supports several possible
    response layouts because the provider's exact
    response schema can vary.
    """

    if not data:
        return None

    # --------------------------------------------------------
    # String
    # --------------------------------------------------------

    if isinstance(
        data,
        str,
    ):

        value = data.strip()

        if _is_http_url(
            value
        ):

            return value

        return None

    # --------------------------------------------------------
    # List
    # --------------------------------------------------------

    if isinstance(
        data,
        list,
    ):

        # Prefer dictionaries containing explicit
        # download/audio URL fields.
        for item in data:

            found = _find_download_url(
                item
            )

            if found:
                return found

        return None

    # --------------------------------------------------------
    # Dictionary
    # --------------------------------------------------------

    if not isinstance(
        data,
        dict,
    ):

        return None

    # Explicit URL keys first.
    for key in URL_KEYS:

        value = data.get(
            key
        )

        if _is_http_url(
            value
        ):

            return value

    # Nested dictionaries/lists.
    for key, value in data.items():

        # Avoid wasting time on huge raw metadata
        # branches that obviously cannot be URLs.
        if key in (
            "success",
            "message",
            "status",
        ):

            continue

        found = _find_download_url(
            value
        )

        if found:
            return found

    return None


# ============================================================
# DOWNLOAD METADATA EXTRACTION
# ============================================================

def _find_first_recursive(
    data,
    keys,
):
    """
    Recursively find the first value matching one
    of the supplied keys.
    """

    if isinstance(
        data,
        dict,
    ):

        for key in keys:

            if key in data:

                value = data.get(
                    key
                )

                if value not in (
                    None,
                    "",
                ):

                    return value

        for value in data.values():

            found = _find_first_recursive(
                value,
                keys,
            )

            if found not in (
                None,
                "",
            ):

                return found

    elif isinstance(
        data,
        list,
    ):

        for item in data:

            found = _find_first_recursive(
                item,
                keys,
            )

            if found not in (
                None,
                "",
            ):

                return found

    return None


# ============================================================
# DOWNLOAD FILE
# ============================================================

def _download_file(
    url: str,
    output_path: Path,
):
    """
    Download the actual audio file returned by
    the Spotify API.
    """

    print(
        "Downloading Spotify audio:",
        url,
    )

    try:

        with SESSION.get(
            url,
            stream=True,
            timeout=SPOTIFY_DOWNLOAD_TIMEOUT,
        ) as response:

            if response.status_code >= 400:

                raise SpotifyDownloadError(
                    "Spotify audio server returned "
                    f"HTTP {response.status_code}."
                )

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with open(
                output_path,
                "wb",
            ) as file:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if chunk:

                        file.write(
                            chunk
                        )

    except SpotifyDownloadError:

        raise

    except requests.RequestException as error:

        raise SpotifyDownloadError(
            f"Could not download Spotify audio: "
            f"{error}"
        ) from error

    if not output_path.exists():

        raise SpotifyDownloadError(
            "Spotify audio file was not created."
        )

    if output_path.stat().st_size == 0:

        raise SpotifyDownloadError(
            "Spotify audio file is empty."
        )

    print(
        "Spotify audio saved:",
        output_path,
        output_path.stat().st_size,
        "bytes",
    )


# ============================================================
# FILE EXTENSION
# ============================================================

def _extension_from_response(
    response_url,
    content_type=None,
):
    """
    Determine the downloaded file extension.
    """

    if content_type:

        content_type = (
            content_type.lower()
            .split(";")[0]
            .strip()
        )

        extension = mimetypes.guess_extension(
            content_type
        )

        if extension:
            return extension

    if response_url:

        try:

            path = urlparse(
                response_url
            ).path

            suffix = Path(
                path
            ).suffix.lower()

            if suffix in (
                ".mp3",
                ".m4a",
                ".aac",
                ".ogg",
                ".opus",
                ".wav",
                ".flac",
                ".webm",
            ):

                return suffix

        except Exception:

            pass

    return ".mp3"


# ============================================================
# SPOTIFY DOWNLOAD
# ============================================================

def download_spotify_song(
    track_id: str,
    output_dir,
):
    """
    Download one Spotify track using the provider's
    /downloadSong endpoint.

    The endpoint accepts either a Spotify track ID
    or Spotify track URL.

    The function normalizes the returned data into
    the structure expected by bot.py.
    """

    if not track_id:

        raise SpotifyDownloadError(
            "Spotify track ID is empty."
        )

    # Accept a URL as well as a raw ID.
    normalized_id = (
        extract_spotify_track_id(
            str(track_id)
        )
        or str(track_id).strip()
    )

    if not normalized_id:

        raise SpotifyDownloadError(
            "Invalid Spotify track ID."
        )

    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    job_dir = (
        output_dir
        / f"spotify_{int(time.time() * 1000)}"
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        params = {
            "songId": normalized_id,
        }

        url = (
            SPOTIFY_API_BASE
            + SPOTIFY_DOWNLOAD_ENDPOINT
        )

        print(
            "Spotify download request:",
            url,
            "songId:",
            normalized_id,
        )

        try:

            response = SESSION.get(
                url,
                headers=_rapidapi_headers(),
                params=params,
                timeout=SPOTIFY_TIMEOUT,
            )

        except requests.RequestException as error:

            raise SpotifyDownloadError(
                f"Spotify download API request failed: "
                f"{error}"
            ) from error

        data = _check_api_response(
            response,
            operation="Spotify download",
        )

        # ----------------------------------------------------
        # Locate actual audio URL
        # ----------------------------------------------------

        download_url = _find_download_url(
            data
        )

        if not download_url:

            print(
                "Spotify download response did not "
                "contain a recognized audio URL."
            )

            try:

                print(
                    "Spotify download response:",
                    json.dumps(
                        data,
                        ensure_ascii=False,
                    )[:5000],
                )

            except Exception:

                print(
                    "Spotify response:",
                    str(data)[:5000],
                )

            raise SpotifyDownloadError(
                "Spotify API returned no usable "
                "audio download URL."
            )

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        title = _find_first_recursive(
            data,
            [
                "title",
                "name",
                "trackName",
                "track_name",
            ],
        )

        artist = _find_first_recursive(
            data,
            [
                "artist",
                "artistName",
                "artist_name",
                "performer",
            ],
        )

        album = _find_first_recursive(
            data,
            [
                "album",
                "albumName",
                "album_name",
            ],
        )

        duration = _find_first_recursive(
            data,
            [
                "duration",
                "duration_ms",
                "durationMs",
            ],
        )

        quality = _find_first_recursive(
            data,
            [
                "quality",
                "bitrate",
                "bit_rate",
            ],
        )

        artwork = _find_first_recursive(
            data,
            [
                "artwork",
                "artworkUrl",
                "artwork_url",
                "cover",
                "coverUrl",
                "cover_url",
                "image",
                "imageUrl",
                "image_url",
            ],
        )

        # ----------------------------------------------------
        # Download audio
        # ----------------------------------------------------

        extension = _extension_from_response(
            download_url
        )

        # Most Spotify downloader APIs return MP3,
        # so preserve that as the default.
        if extension == ".bin":
            extension = ".mp3"

        safe_title = re.sub(
            r'[\\/:*?"<>|]+',
            "_",
            str(
                title
                or "spotify_audio"
            ),
        ).strip()

        if not safe_title:

            safe_title = "spotify_audio"

        filename = (
            f"{safe_title}"
            f"{extension}"
        )

        output_path = (
            job_dir
            / filename
        )

        _download_file(
            download_url,
            output_path,
        )

        # ----------------------------------------------------
        # Normalize duration
        # ----------------------------------------------------

        if duration:

            try:

                duration = float(
                    duration
                )

                if duration > 10000:

                    duration = (
                        duration / 1000
                    )

                duration = int(
                    duration
                )

            except (
                TypeError,
                ValueError,
            ):

                duration = None

        # ----------------------------------------------------
        # Normalize quality
        # ----------------------------------------------------

        if quality:

            quality = str(
                quality
            )

        else:

            quality = "Spotify Audio"

        # ----------------------------------------------------
        # Normalize artist
        # ----------------------------------------------------

        if isinstance(
            artist,
            dict,
        ):

            artist = _first_value(
                artist,
                [
                    "name",
                    "artist",
                ],
                "",
            )

        if isinstance(
            artist,
            list,
        ):

            names = []

            for item in artist:

                if isinstance(
                    item,
                    str,
                ):

                    names.append(
                        item
                    )

                elif isinstance(
                    item,
                    dict,
                ):

                    name = _first_value(
                        item,
                        [
                            "name",
                            "artist",
                        ],
                    )

                    if name:
                        names.append(
                            str(name)
                        )

            artist = ", ".join(
                names
            )

        title = str(
            title
            or "Spotify Audio"
        )

        artist = str(
            artist
            or "Unknown Artist"
        )

        album = str(
            album
            or ""
        )

        return {
            "path": str(
                output_path
            ),
            "filename": filename,
            "title": title,
            "artist": artist,
            "album": album,
            "duration": duration,
            "quality": quality,
            "artwork": artwork,
            "job_dir": str(
                job_dir
            ),
            "source": "spotify",
            "spotify_id": normalized_id,
        }

    except Exception:

        # If anything fails, remove the temporary
        # Spotify job directory.
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        raise


# ============================================================
# SIMPLE SELF TEST
# ============================================================

if __name__ == "__main__":

    print(
        "Spotify module loaded."
    )

    print(
        "RapidAPI host:",
        SPOTIFY_API_HOST,
    )

    print(
        "Search endpoint:",
        SPOTIFY_SEARCH_ENDPOINT,
    )

    print(
        "Download endpoint:",
        SPOTIFY_DOWNLOAD_ENDPOINT,
    )

    print(
        "API key configured:",
        bool(SPOTIFY_API_KEY),
    )