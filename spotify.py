"""
Audio-Bot Spotify integration
API: spotify-api40.p.rapidapi.com

Supported:
    GET /search
    GET /track

Environment variables:
    SPOTIFY_API40_KEY
    SPOTIFY_API40_HOST

Public functions used by bot.py:
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

SPOTIFY_API40_KEY = os.getenv("SPOTIFY_API40_KEY", "").strip()

SPOTIFY_API40_HOST = os.getenv(
    "SPOTIFY_API40_HOST",
    "spotify-api40.p.rapidapi.com",
).strip()

BASE_URL = f"https://{SPOTIFY_API40_HOST}"

SEARCH_URL = f"{BASE_URL}/search"
TRACK_URL = f"{BASE_URL}/track"

REQUEST_TIMEOUT = int(
    os.getenv("SPOTIFY_API40_TIMEOUT", "60")
)

DOWNLOAD_TIMEOUT = int(
    os.getenv("SPOTIFY_API40_DOWNLOAD_TIMEOUT", "300")
)

USER_AGENT = "Audio-Bot/3.0"

MAX_FILENAME_LENGTH = 180


# ============================================================
# EXCEPTIONS
# ============================================================


class SpotifyError(Exception):
    pass


class SpotifyAPIError(SpotifyError):
    pass


class SpotifyDownloadError(SpotifyError):
    pass


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


def _headers() -> dict[str, str]:
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
# BASIC HELPERS
# ============================================================


def _text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    return str(value).strip()


def _is_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False

    try:
        parsed = urlparse(value)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:
        return False


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _api_request(
    url: str,
    params: dict[str, Any],
    timeout: int = REQUEST_TIMEOUT,
) -> Any:

    try:
        response = session.get(
            url,
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


# ============================================================
# RECURSIVE JSON HELPERS
# ============================================================


def _find_key_values(
    obj: Any,
    keys: set[str],
) -> list[Any]:

    results: list[Any] = []

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

    wanted = set(keys)

    values = _find_key_values(
        obj,
        wanted,
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


def _all_urls(obj: Any) -> list[str]:

    urls: list[str] = []

    if isinstance(obj, str):

        if _is_url(obj):
            urls.append(obj)

        return urls

    if isinstance(obj, dict):

        for value in obj.values():
            urls.extend(
                _all_urls(value)
            )

    elif isinstance(obj, list):

        for item in obj:
            urls.extend(
                _all_urls(item)
            )

    return urls


# ============================================================
# SPOTIFY ID / URL HELPERS
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

    # open.spotify.com/track/ID
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

    # Spotify URI
    match = re.search(
        r"spotify:track:([A-Za-z0-9]+)",
        value,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    # Spotify URL
    track_id = extract_spotify_track_id(value)

    if track_id:
        return track_id

    # Plain ID
    if re.fullmatch(
        r"[A-Za-z0-9]{10,64}",
        value,
    ):
        return value

    return ""


def is_spotify_url(text: str) -> bool:

    if not text:
        return False

    return bool(
        re.search(
            r"https?://(?:open\.)?spotify\.com/",
            text,
            re.IGNORECASE,
        )
    )


# ============================================================
# TRACK OBJECT DETECTION
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

    # Most reliable checks.
    if typename == "track":
        return True

    if object_type == "track":
        return True

    if uri.startswith("spotify:track:"):
        return True

    # Do NOT classify arbitrary objects containing "id"
    # as tracks. This was the bug in the previous version.
    return False


def _find_track_objects(
    obj: Any,
) -> list[dict[str, Any]]:

    tracks: list[dict[str, Any]] = []

    if isinstance(obj, dict):

        if _is_track_object(obj):
            tracks.append(obj)

        for value in obj.values():

            tracks.extend(
                _find_track_objects(value)
            )

    elif isinstance(obj, list):

        for item in obj:

            tracks.extend(
                _find_track_objects(item)
            )

    return tracks


# ============================================================
# TRACK METADATA
# ============================================================


def _extract_track_id(
    track: dict[str, Any],
) -> str:

    # First use URI because it explicitly tells us that
    # the object is a track.
    uri = _text(
        track.get("uri")
    )

    if uri.startswith(
        "spotify:track:"
    ):

        return _normalize_track_id(uri)

    # Standard API style.
    for key in (
        "id",
        "trackId",
        "track_id",
    ):

        value = track.get(key)

        normalized = _normalize_track_id(
            value
        )

        if normalized:
            return normalized

    return ""


def _extract_name(
    track: dict[str, Any],
) -> str:

    for key in (
        "name",
        "title",
        "trackName",
        "track_name",
    ):

        value = track.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return "Unknown Track"


def _extract_artists(
    track: dict[str, Any],
) -> str:

    artists = track.get(
        "artists"
    )

    names: list[str] = []

    if isinstance(artists, list):

        for artist in artists:

            if not isinstance(
                artist,
                dict,
            ):
                continue

            # Standard Spotify object.
            name = artist.get("name")

            if name:
                names.append(
                    _text(name)
                )
                continue

            # API40 structure:
            # artists -> profile -> name
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

    elif isinstance(artists, dict):

        name = artists.get("name")

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

    # API40 may expose artist directly.
    if not names:

        artist = track.get(
            "artist"
        )

        if isinstance(
            artist,
            str,
        ):
            names.append(
                artist.strip()
            )

        elif isinstance(
            artist,
            dict,
        ):

            name = artist.get(
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

    return ", ".join(unique) or "Unknown Artist"


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

        name = album.get(
            "name"
        )

        if name:
            return _text(name)

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

        if value > 10000:
            return int(
                value / 1000
            )

        return int(value)

    except (
        TypeError,
        ValueError,
    ):
        return None


def _extract_artwork(
    track: dict[str, Any],
) -> str | None:

    # Standard Spotify structure.
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

                if isinstance(
                    image,
                    dict,
                ):

                    url = image.get(
                        "url"
                    )

                    if _is_url(url):
                        return url

    # API40 coverArt structure.
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

            # Prefer largest image.
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

                if not _is_url(url):
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

    name = _extract_name(
        track
    )

    artist = _extract_artists(
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

    external_urls = track.get(
        "external_urls"
    )

    if isinstance(
        external_urls,
        dict,
    ):

        spotify_url = _text(
            external_urls.get(
                "spotify"
            )
        )

    if not spotify_url:

        uri = _text(
            track.get(
                "uri"
            )
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
        "title": name,
        "name": name,
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
# SPOTIFY SEARCH
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

    # API40 screenshot confirms /search uses:
    #
    # query = search query
    #
    # Unlike the standard Spotify Web API, do not add
    # unsupported parameters such as type/offset/limit
    # unless the RapidAPI product documents them.
    data = _api_request(
        SEARCH_URL,
        {
            "query": query,
        },
    )

    # --------------------------------------------------------
    # CRITICAL:
    # Only objects explicitly identified as Tracks.
    #
    # This prevents Album objects such as:
    #
    # __typename: "Album"
    # uri: spotify:album:...
    #
    # from being passed to /track.
    # --------------------------------------------------------

    track_objects = _find_track_objects(
        data
    )

    results: list[dict[str, Any]] = []

    seen_ids: set[str] = set()

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
        f"[Spotify] Found {len(results)} track(s)"
    )

    if not results:

        print(
            "[Spotify] No Track objects found "
            "in /search response."
        )

        # Useful for debugging API schema changes.
        try:
            print(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=2,
                )[:8000]
            )
        except Exception:
            pass

    return {
        "query": query,
        "results": results,
        "raw": data,
    }


# ============================================================
# AUDIO URL EXTRACTION
# ============================================================


AUDIO_KEYS = {
    "downloadUrl",
    "download_url",
    "download",
    "downloadLink",
    "download_link",

    "audioUrl",
    "audio_url",
    "audio",
    "audioLink",
    "audio_link",

    "mediaUrl",
    "media_url",
    "media",

    "streamUrl",
    "stream_url",
    "stream",

    "fileUrl",
    "file_url",
    "file",

    "playUrl",
    "play_url",

    "playbackUrl",
    "playback_url",

    "url",
    "href",
    "link",
    "src",
}


def _audio_score(
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
        ".webm",
        "audio",
        "download",
        "media",
        "stream",
        "cdn",
    ):

        if token in lower:
            score += 10

    # Never mistake Spotify metadata/image URLs
    # for downloadable audio.
    if "open.spotify.com" in lower:
        score -= 100

    if "i.scdn.co" in lower:
        score -= 100

    if "/image/" in lower:
        score -= 100

    return score


def _find_audio_url(
    data: Any,
) -> str | None:

    candidates: list[str] = []

    # --------------------------------------------------------
    # First inspect explicit audio-related keys.
    # --------------------------------------------------------

    values = _find_key_values(
        data,
        AUDIO_KEYS,
    )

    for value in values:

        if isinstance(
            value,
            str,
        ):

            if _is_url(value):
                candidates.append(
                    value
                )

        elif isinstance(
            value,
            dict,
        ):

            for url in _all_urls(
                value
            ):

                candidates.append(
                    url
                )

        elif isinstance(
            value,
            list,
        ):

            for url in _all_urls(
                value
            ):

                candidates.append(
                    url
                )

    # --------------------------------------------------------
    # Also scan every URL in response.
    # --------------------------------------------------------

    candidates.extend(
        _all_urls(data)
    )

    # Deduplicate.
    unique: list[str] = []

    for url in candidates:

        if url not in unique:
            unique.append(url)

    if not unique:
        return None

    unique.sort(
        key=_audio_score,
        reverse=True,
    )

    for url in unique:

        if _audio_score(url) <= -50:
            continue

        return url

    return None


# ============================================================
# FILE HELPERS
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
        value = value[
            :MAX_FILENAME_LENGTH
        ].rstrip()

    return value or "spotify_audio"


def _extension_from_url(
    url: str,
) -> str:

    try:

        path = urlparse(
            url
        ).path

        extension = Path(
            path
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


def _create_job_dir(
    output_dir: str | Path,
    track_id: str,
) -> Path:

    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    job_dir = (
        output_dir
        / f"spotify_{track_id}_{int(time.time() * 1000)}"
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return job_dir


# ============================================================
# FILE DOWNLOAD
# ============================================================


def _download_audio(
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
                "[Spotify] Download HTTP:",
                response.status_code,
            )

            print(
                "[Spotify] Content-Type:",
                content_type,
            )

            if content_length:
                print(
                    "[Spotify] Content-Length:",
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
                        file.write(
                            chunk
                        )

        if not temporary.exists():
            raise SpotifyDownloadError(
                "Downloaded file does not exist."
            )

        if temporary.stat().st_size <= 0:
            raise SpotifyDownloadError(
                "Downloaded file is empty."
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
            f"Audio download failed: {exc}"
        ) from exc


# ============================================================
# MAIN SPOTIFY DOWNLOAD
# ============================================================


def download_spotify_song(
    track_id: str,
    output_dir: str | Path,
) -> dict[str, Any]:

    normalized_id = _normalize_track_id(
        track_id
    )

    if not normalized_id:
        raise SpotifyDownloadError(
            "Invalid Spotify track ID."
        )

    print(
        "[Spotify] Downloading track:",
        normalized_id,
    )

    spotify_url = (
        "https://open.spotify.com/track/"
        + normalized_id
    )

    # ========================================================
    # IMPORTANT
    #
    # API40 documents both:
    #
    #   /track?id=...
    #   /track?url=...
    #
    # We try URL first because it explicitly identifies
    # the resource as a Spotify TRACK.
    # ========================================================

    data = None

    try:

        print(
            "[Spotify] Trying /track with URL..."
        )

        data = _api_request(
            TRACK_URL,
            {
                "url": spotify_url,
            },
        )

    except SpotifyAPIError as first_error:

        print(
            "[Spotify] URL lookup failed:",
            first_error,
        )

        print(
            "[Spotify] Falling back to track ID..."
        )

        data = _api_request(
            TRACK_URL,
            {
                "id": normalized_id,
            },
        )

    # ========================================================
    # API success check
    # ========================================================

    if isinstance(
        data,
        dict,
    ):

        if data.get(
            "success"
        ) is False:

            message = (
                data.get("message")
                or data.get("error")
                or "Spotify /track failed."
            )

            raise SpotifyDownloadError(
                _text(message)
            )

    # ========================================================
    # Find the actual Track object
    # ========================================================

    track_objects = _find_track_objects(
        data
    )

    track_object = (
        track_objects[0]
        if track_objects
        else {}
    )

    # ========================================================
    # Metadata
    # ========================================================

    if track_object:

        title = _extract_name(
            track_object
        )

        artist = _extract_artists(
            track_object
        )

        album = _extract_album(
            track_object
        )

        duration = _extract_duration(
            track_object
        )

        artwork = _extract_artwork(
            track_object
        )

    else:

        # Fallback for APIs where /track wraps metadata
        # differently.
        title = _text(
            _find_first(
                data,
                (
                    "title",
                    "name",
                    "trackName",
                    "track_name",
                ),
            )
        )

        artist_value = _find_first(
            data,
            (
                "artist",
                "artists",
                "artistName",
                "artist_name",
            ),
        )

        artist = _text(
            artist_value
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

        duration_value = _find_first(
            data,
            (
                "duration_ms",
                "durationMs",
                "duration",
            ),
        )

        try:

            duration = int(
                float(duration_value)
            )

            if duration > 10000:
                duration //= 1000

        except Exception:
            duration = None

        artwork = None

    if not title:
        title = (
            f"Spotify Track {normalized_id}"
        )

    if not artist:
        artist = "Unknown Artist"

    # ========================================================
    # Extract actual audio URL
    # ========================================================

    audio_url = _find_audio_url(
        data
    )

    if not audio_url:

        print(
            "[Spotify] No audio URL found."
        )

        print(
            "[Spotify] /track response:"
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
            "Spotify /track response did not "
            "contain a usable audio URL."
        )

    print(
        "[Spotify] Audio URL found."
    )

    # ========================================================
    # Quality
    # ========================================================

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

    # ========================================================
    # Job directory
    # ========================================================

    job_dir = _create_job_dir(
        output_dir,
        normalized_id,
    )

    # ========================================================
    # Filename
    # ========================================================

    filename_base = _safe_filename(
        f"{artist} - {title}"
    )

    extension = _extension_from_url(
        audio_url
    )

    if not extension:
        extension = ".audio"

    output_path = (
        job_dir
        / f"{filename_base}{extension}"
    )

    # ========================================================
    # Download
    # ========================================================

    content_type = _download_audio(
        audio_url,
        output_path,
    )

    # ========================================================
    # Correct extension if needed
    # ========================================================

    if output_path.suffix == ".audio":

        content_extension = (
            _extension_from_content_type(
                content_type
            )
        )

        if content_extension:

            corrected_path = (
                job_dir
                / f"{filename_base}"
                f"{content_extension}"
            )

            output_path.replace(
                corrected_path
            )

            output_path = corrected_path

    # ========================================================
    # Validate
    # ========================================================

    if not output_path.exists():
        raise SpotifyDownloadError(
            "Spotify audio file was not created."
        )

    file_size = output_path.stat().st_size

    if file_size <= 0:
        raise SpotifyDownloadError(
            "Spotify audio file is empty."
        )

    print(
        "[Spotify] Download complete:"
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
# CONFIG CHECK
# ============================================================


def spotify_configured() -> bool:

    return bool(
        SPOTIFY_API40_KEY
        and SPOTIFY_API40_HOST
    )


# ============================================================
# LOCAL TEST
# ============================================================


if __name__ == "__main__":

    print(
        "===================================="
    )

    print(
        "Spotify API40 configuration"
    )

    print(
        "Host:",
        SPOTIFY_API40_HOST,
    )

    print(
        "Key configured:",
        bool(SPOTIFY_API40_KEY),
    )

    print(
        "===================================="
    )