"""
Spotify integration for Audio Bot.

Architecture:

    Spotify API40
        |
        | /search
        | /track
        v
    Track metadata + Spotify ID
        |
        v
    Spotify Downloader9
        |
        | /downloadSong?songId=<spotify_id>
        v
    Full audio download

RENDER VARIABLES:

    SPOTIFY_API40_HOST
    SPOTIFY_API40_KEY

    RAPIDAPI_HOST
    RAPIDAPI_KEY
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
# API 40 - Spotify search / metadata
# ------------------------------------------------------------

SPOTIFY_API40_HOST = os.getenv(
    "SPOTIFY_API40_HOST",
    "spotify-api40.p.rapidapi.com",
).strip()

SPOTIFY_API40_KEY = os.getenv(
    "SPOTIFY_API40_KEY",
    "",
).strip()


# ------------------------------------------------------------
# FIRST API - Spotify Downloader9
# ------------------------------------------------------------

RAPIDAPI_HOST = os.getenv(
    "RAPIDAPI_HOST",
    "spotify-downloader9.p.rapidapi.com",
).strip()

RAPIDAPI_KEY = os.getenv(
    "RAPIDAPI_KEY",
    "",
).strip()


# ------------------------------------------------------------
# URLs
# ------------------------------------------------------------

API40_BASE_URL = (
    f"https://{SPOTIFY_API40_HOST}"
)

API40_SEARCH_URL = (
    f"{API40_BASE_URL}/search"
)

API40_TRACK_URL = (
    f"{API40_BASE_URL}/track"
)

DOWNLOADER_BASE_URL = (
    f"https://{RAPIDAPI_HOST}"
)

DOWNLOADER_URL = (
    f"{DOWNLOADER_BASE_URL}/downloadSong"
)


# ============================================================
# TIMEOUTS
# ============================================================

API_TIMEOUT = int(
    os.getenv(
        "SPOTIFY_API_TIMEOUT",
        "60",
    )
)

DOWNLOAD_TIMEOUT = int(
    os.getenv(
        "SPOTIFY_DOWNLOAD_TIMEOUT",
        "300",
    )
)


USER_AGENT = (
    "Audio-Bot/3.0 "
    "(Telegram Spotify Downloader)"
)


MAX_FILENAME_LENGTH = 180


# ============================================================
# EXCEPTIONS
# ============================================================


class SpotifyError(Exception):
    """Base Spotify error."""


class SpotifyAPIError(SpotifyError):
    """Spotify API error."""


class SpotifyDownloadError(SpotifyError):
    """Spotify download error."""


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
# BASIC HELPERS
# ============================================================


def _text(
    value: Any,
) -> str:

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    return str(value).strip()


def _is_http_url(
    value: Any,
) -> bool:

    if not isinstance(
        value,
        str,
    ):
        return False

    try:

        parsed = urlparse(value)

        return (
            parsed.scheme in {
                "http",
                "https",
            }
            and bool(
                parsed.netloc
            )
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


# ============================================================
# SPOTIFY ID
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
        r"open\.spotify\.com/"
        r"(?:intl-[^/]+/)?"
        r"track/"
        r"([A-Za-z0-9]+)",
        value,
        re.IGNORECASE,
    )

    if match:

        return match.group(1)

    # Plain Spotify ID
    if re.fullmatch(
        r"[A-Za-z0-9]{10,64}",
        value,
    ):

        return value

    return None


def _normalize_track_id(
    value: Any,
) -> str:

    value = _text(value)

    if not value:
        return ""

    track_id = (
        extract_spotify_track_id(
            value
        )
    )

    return track_id or ""


# ============================================================
# RAPIDAPI HEADERS
# ============================================================


def _api40_headers() -> dict[str, str]:

    if not SPOTIFY_API40_KEY:

        raise SpotifyAPIError(
            "SPOTIFY_API40_KEY is not configured."
        )

    return {
        "x-rapidapi-key":
            SPOTIFY_API40_KEY,

        "x-rapidapi-host":
            SPOTIFY_API40_HOST,

        "Accept":
            "application/json",

        "User-Agent":
            USER_AGENT,
    }


def _downloader_headers() -> dict[str, str]:

    if not RAPIDAPI_KEY:

        raise SpotifyDownloadError(
            "RAPIDAPI_KEY is not configured."
        )

    return {
        "x-rapidapi-key":
            RAPIDAPI_KEY,

        "x-rapidapi-host":
            RAPIDAPI_HOST,

        "Accept":
            "application/json",

        "User-Agent":
            USER_AGENT,
    }


# ============================================================
# API 40 REQUEST
# ============================================================


def _api40_get(
    endpoint: str,
    params: dict[str, Any],
) -> Any:

    headers = _api40_headers()

    try:

        response = session.get(
            endpoint,
            headers=headers,
            params=params,
            timeout=API_TIMEOUT,
        )

    except requests.RequestException as exc:

        raise SpotifyAPIError(
            f"Spotify API request failed: {exc}"
        ) from exc

    data = _safe_json(
        response
    )

    if response.status_code >= 400:

        message = ""

        if isinstance(
            data,
            dict,
        ):

            message = (
                data.get("message")
                or data.get("error")
                or data.get("detail")
                or ""
            )

        if not message:

            message = (
                response.text[:500]
            )

        raise SpotifyAPIError(
            "Spotify API returned "
            f"HTTP {response.status_code}: "
            f"{message}"
        )

    if data is None:

        raise SpotifyAPIError(
            "Spotify API returned invalid JSON."
        )

    return data


# ============================================================
# API 40 TRACK REQUEST
# ============================================================


def get_spotify_track(
    track_id: str,
) -> dict[str, Any] | None:

    normalized_id = (
        _normalize_track_id(
            track_id
        )
    )

    if not normalized_id:
        return None

    spotify_url = (
        "https://open.spotify.com/track/"
        + normalized_id
    )

    print(
        "[Spotify] Fetching track metadata:",
        normalized_id,
    )

    try:

        data = _api40_get(
            API40_TRACK_URL,
            {
                "id": normalized_id,
                "url": spotify_url,
            },
        )

    except SpotifyAPIError as exc:

        print(
            "[Spotify] Track metadata "
            "request failed:",
            exc,
        )

        return None

    track = _extract_track_from_response(
        data
    )

    if not track:

        return None

    normalized = _normalize_track(
        track
    )

    if not normalized.get("id"):

        normalized["id"] = (
            normalized_id
        )

        normalized["track_id"] = (
            normalized_id
        )

        normalized["spotify_id"] = (
            normalized_id
        )

    return normalized


# ============================================================
# RECURSIVE JSON SEARCH
# ============================================================


def _find_key_values(
    obj: Any,
    keys: set[str],
) -> list[Any]:

    results = []

    if isinstance(
        obj,
        dict,
    ):

        for key, value in obj.items():

            if key in keys:

                results.append(
                    value
                )

            results.extend(
                _find_key_values(
                    value,
                    keys,
                )
            )

    elif isinstance(
        obj,
        list,
    ):

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


def _find_track_objects(
    obj: Any,
) -> list[dict[str, Any]]:

    tracks = []

    if isinstance(
        obj,
        dict,
    ):

        typename = _text(
            obj.get(
                "__typename"
            )
        ).lower()

        object_type = _text(
            obj.get(
                "type"
            )
        ).lower()

        uri = _text(
            obj.get(
                "uri"
            )
        ).lower()

        if (
            typename == "track"
            or object_type == "track"
            or uri.startswith(
                "spotify:track:"
            )
        ):

            tracks.append(
                obj
            )

        for value in obj.values():

            tracks.extend(
                _find_track_objects(
                    value
                )
            )

    elif isinstance(
        obj,
        list,
    ):

        for item in obj:

            tracks.extend(
                _find_track_objects(
                    item
                )
            )

    return tracks


# ============================================================
# API40 INTERNAL TRACK FORMAT
# ============================================================


def _unwrap_track(
    obj: Any,
) -> dict[str, Any] | None:

    if not isinstance(
        obj,
        dict,
    ):

        return None

    # API40 search result:
    #
    # item.data
    #
    # or
    #
    # itemV2.data

    for key in (
        "item",
        "itemV2",
        "track",
        "data",
    ):

        value = obj.get(
            key
        )

        if isinstance(
            value,
            dict,
        ):

            nested = _unwrap_track(
                value
            )

            if nested:

                return nested

    # If this looks like the actual
    # Spotify track data, return it.

    if any(
        key in obj
        for key in (
            "trackDuration",
            "duration",
            "albumOfTrack",
            "artists",
            "name",
            "uri",
        )
    ):

        uri = _text(
            obj.get("uri")
        )

        if (
            uri.startswith(
                "spotify:track:"
            )
            or obj.get("name")
            or obj.get("id")
        ):

            return obj

    return None


def _extract_track_from_response(
    data: Any,
) -> dict[str, Any] | None:

    # First look for explicit Track objects.
    tracks = _find_track_objects(
        data
    )

    if tracks:

        return tracks[0]

    # API40 searchV2:
    #
    # data.searchV2.tracksV2.items

    search_v2 = None

    if isinstance(
        data,
        dict,
    ):

        root_data = data.get(
            "data"
        )

        if isinstance(
            root_data,
            dict,
        ):

            search_v2 = (
                root_data.get(
                    "searchV2"
                )
            )

    if isinstance(
        search_v2,
        dict,
    ):

        for tracks_key in (
            "tracksV2",
            "tracks",
        ):

            tracks_data = (
                search_v2.get(
                    tracks_key
                )
            )

            if not isinstance(
                tracks_data,
                dict,
            ):

                continue

            items = (
                tracks_data.get(
                    "items"
                )
                or []
            )

            if isinstance(
                items,
                list,
            ):

                for item in items:

                    track = (
                        _unwrap_track(
                            item
                        )
                    )

                    if track:

                        return track

    # Generic fallback.
    if isinstance(
        data,
        list,
    ):

        for item in data:

            track = (
                _unwrap_track(
                    item
                )
            )

            if track:

                return track

    return None


# ============================================================
# TRACK METADATA EXTRACTION
# ============================================================


def _extract_track_id_from_object(
    track: dict[str, Any],
) -> str:

    # Direct ID.
    for key in (
        "id",
        "trackId",
        "track_id",
    ):

        value = _normalize_track_id(
            track.get(
                key
            )
        )

        if value:

            return value

    # URI.
    uri = _text(
        track.get(
            "uri"
        )
    )

    if uri:

        value = (
            _normalize_track_id(
                uri
            )
        )

        if value:

            return value

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

        value = _text(
            track.get(
                key
            )
        )

        if value:

            return value

    return "Unknown Track"


def _extract_artist(
    track: dict[str, Any],
) -> str:

    names = []

    # --------------------------------------------------------
    # Standard Spotify:
    #
    # artists: [
    #   {"name": "..."}
    # ]
    # --------------------------------------------------------

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

            name = _text(
                artist.get(
                    "name"
                )
            )

            if not name:

                profile = artist.get(
                    "profile"
                )

                if isinstance(
                    profile,
                    dict,
                ):

                    name = _text(
                        profile.get(
                            "name"
                        )
                    )

            if name:

                names.append(
                    name
                )

    # --------------------------------------------------------
    # API40:
    #
    # artists.items[].profile.name
    # --------------------------------------------------------

    if isinstance(
        artists,
        dict,
    ):

        items = (
            artists.get(
                "items"
            )
            or []
        )

        if isinstance(
            items,
            list,
        ):

            for artist in items:

                if not isinstance(
                    artist,
                    dict,
                ):

                    continue

                profile = artist.get(
                    "profile"
                )

                if isinstance(
                    profile,
                    dict,
                ):

                    name = _text(
                        profile.get(
                            "name"
                        )
                    )

                    if name:

                        names.append(
                            name
                        )

                name = _text(
                    artist.get(
                        "name"
                    )
                )

                if name:

                    names.append(
                        name
                    )

        name = _text(
            artists.get(
                "name"
            )
        )

        if name:

            names.append(
                name
            )

    # --------------------------------------------------------
    # API40 can expose artist information
    # in artistOfTrack / artistsV2.
    # --------------------------------------------------------

    for key in (
        "artistOfTrack",
        "artist",
        "artistsV2",
    ):

        value = track.get(
            key
        )

        if isinstance(
            value,
            dict,
        ):

            profile = value.get(
                "profile"
            )

            if isinstance(
                profile,
                dict,
            ):

                name = _text(
                    profile.get(
                        "name"
                    )
                )

                if name:

                    names.append(
                        name
                    )

            name = _text(
                value.get(
                    "name"
                )
            )

            if name:

                names.append(
                    name
                )

            items = value.get(
                "items"
            )

            if isinstance(
                items,
                list,
            ):

                for item in items:

                    if not isinstance(
                        item,
                        dict,
                    ):

                        continue

                    profile = item.get(
                        "profile"
                    )

                    if isinstance(
                        profile,
                        dict,
                    ):

                        name = _text(
                            profile.get(
                                "name"
                            )
                        )

                        if name:

                            names.append(
                                name
                            )

    # Remove duplicates.
    unique = []

    for name in names:

        if (
            name
            and name not in unique
        ):

            unique.append(
                name
            )

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

        value = _text(
            album.get(
                "name"
            )
            or album.get(
                "title"
            )
        )

        if value:

            return value

    # API40.
    album_of_track = track.get(
        "albumOfTrack"
    )

    if isinstance(
        album_of_track,
        dict,
    ):

        return _text(
            album_of_track.get(
                "name"
            )
        )

    return ""


def _extract_duration(
    track: dict[str, Any],
) -> int | None:

    # Standard Spotify.
    values = [
        track.get(
            "duration_ms"
        ),
        track.get(
            "durationMs"
        ),
        track.get(
            "duration"
        ),
    ]

    # API40:
    #
    # trackDuration.totalMilliseconds
    track_duration = track.get(
        "trackDuration"
    )

    if isinstance(
        track_duration,
        dict,
    ):

        values.append(
            track_duration.get(
                "totalMilliseconds"
            )
        )

    duration = None

    for value in values:

        if value is None:
            continue

        try:

            duration = float(
                value
            )

            break

        except (
            TypeError,
            ValueError,
        ):

            continue

    if duration is None:
        return None

    # Milliseconds -> seconds.
    if duration > 10000:

        duration /= 1000

    return int(
        duration
    )


def _extract_artwork(
    track: dict[str, Any],
) -> str | None:

    # Standard Spotify.
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

                if _is_http_url(
                    url
                ):

                    return url

    # API40.
    for album_key in (
        "albumOfTrack",
        "album",
    ):

        album_data = track.get(
            album_key
        )

        if not isinstance(
            album_data,
            dict,
        ):

            continue

        cover_art = (
            album_data.get(
                "coverArt"
            )
        )

        if not isinstance(
            cover_art,
            dict,
        ):

            continue

        sources = (
            cover_art.get(
                "sources"
            )
            or []
        )

        candidates = []

        if isinstance(
            sources,
            list,
        ):

            for source in sources:

                if not isinstance(
                    source,
                    dict,
                ):

                    continue

                url = source.get(
                    "url"
                )

                if not _is_http_url(
                    url
                ):

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

    track_id = (
        _extract_track_id_from_object(
            track
        )
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

    if track_id:

        spotify_url = (
            "https://open.spotify.com/track/"
            + track_id
        )

    return {
        "id":
            track_id,

        "track_id":
            track_id,

        "spotify_id":
            track_id,

        "title":
            title,

        "name":
            title,

        "artist":
            artist,

        "artists":
            artist,

        "album":
            album,

        "duration":
            duration,

        "artwork":
            artwork,

        "image":
            artwork,

        "url":
            spotify_url,

        "raw":
            track,
    }


# ============================================================
# SEARCH
# ============================================================


def search_spotify(
    query: str,
    limit: int = 8,
) -> dict[str, Any]:

    query = _text(
        query
    )

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
            10,
        ),
    )

    print(
        "[Spotify] Searching:",
        query,
    )

    data = _api40_get(
        API40_SEARCH_URL,
        {
            "query": query,
        },
    )

    results = []

    seen_ids = set()

    # --------------------------------------------------------
    # Primary API40 searchV2 parser
    # --------------------------------------------------------

    root_data = (
        data.get("data")
        if isinstance(
            data,
            dict,
        )
        else None
    )

    search_v2 = (
        root_data.get(
            "searchV2"
        )
        if isinstance(
            root_data,
            dict,
        )
        else None
    )

    if isinstance(
        search_v2,
        dict,
    ):

        tracks_container = (
            search_v2.get(
                "tracksV2"
            )
            or search_v2.get(
                "tracks"
            )
        )

        if isinstance(
            tracks_container,
            dict,
        ):

            items = (
                tracks_container.get(
                    "items"
                )
                or []
            )

            if isinstance(
                items,
                list,
            ):

                for item in items:

                    track = (
                        _unwrap_track(
                            item
                        )
                    )

                    if not track:
                        continue

                    normalized = (
                        _normalize_track(
                            track
                        )
                    )

                    track_id = (
                        normalized.get(
                            "id"
                        )
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

                    if len(
                        results
                    ) >= limit:

                        break

    # --------------------------------------------------------
    # Generic fallback
    # --------------------------------------------------------

    if len(results) < limit:

        track_objects = (
            _find_track_objects(
                data
            )
        )

        for track in track_objects:

            normalized = (
                _normalize_track(
                    track
                )
            )

            track_id = (
                normalized.get(
                    "id"
                )
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

            if len(
                results
            ) >= limit:

                break

    print(
        "[Spotify] Found",
        len(results),
        "track(s)",
    )

    if not results:

        print(
            "[Spotify] No tracks found "
            "in API40 response."
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

            pass

    return {
        "query":
            query,

        "results":
            results,

        "raw":
            data,
    }


# ============================================================
# DOWNLOADER API
# ============================================================


def _request_downloader(
    track_id: str,
) -> dict[str, Any]:

    headers = (
        _downloader_headers()
    )

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
        "[Spotify] Downloader9:",
        normalized_id,
    )

    try:

        response = session.get(
            DOWNLOADER_URL,
            headers=headers,
            params={
                "songId":
                    normalized_id,
            },
            timeout=API_TIMEOUT,
        )

    except requests.RequestException as exc:

        raise SpotifyDownloadError(
            "Spotify downloader request "
            f"failed: {exc}"
        ) from exc

    data = _safe_json(
        response
    )

    if response.status_code >= 400:

        message = ""

        if isinstance(
            data,
            dict,
        ):

            message = (
                data.get(
                    "message"
                )
                or data.get(
                    "error"
                )
                or data.get(
                    "detail"
                )
                or ""
            )

        if not message:

            message = (
                response.text[:500]
            )

        raise SpotifyDownloadError(
            "Spotify Downloader9 returned "
            f"HTTP {response.status_code}: "
            f"{message}"
        )

    if not isinstance(
        data,
        dict,
    ):

        raise SpotifyDownloadError(
            "Spotify Downloader9 returned "
            "an invalid response."
        )

    # Expected structure:
    #
    # {
    #   "success": true,
    #   "data": {
    #       "title": "...",
    #       "artist": "...",
    #       "album": "...",
    #       "cover": "...",
    #       "downloadLink": "..."
    #   }
    # }

    return data


# ============================================================
# DOWNLOAD URL
# ============================================================


def _extract_downloader_data(
    response: dict[str, Any],
) -> dict[str, Any]:

    data = response.get(
        "data"
    )

    if isinstance(
        data,
        dict,
    ):

        return data

    # Some API wrappers can nest
    # the actual result.
    for key in (
        "result",
        "response",
    ):

        value = response.get(
            key
        )

        if isinstance(
            value,
            dict,
        ):

            nested = (
                value.get(
                    "data"
                )
            )

            if isinstance(
                nested,
                dict,
            ):

                return nested

            return value

    return {}


def _extract_download_url(
    data: dict[str, Any],
) -> str:

    # The Spotify Downloader9 API
    # uses downloadLink.
    for key in (
        "downloadLink",
        "download_link",
        "downloadUrl",
        "download_url",
        "url",
        "audioUrl",
        "audio_url",
        "fileUrl",
        "file_url",
        "link",
    ):

        value = data.get(
            key
        )

        if _is_http_url(
            value
        ):

            return value

    # Recursive fallback.
    values = _find_key_values(
        data,
        {
            "downloadLink",
            "download_link",
            "downloadUrl",
            "download_url",
            "url",
            "audioUrl",
            "audio_url",
            "fileUrl",
            "file_url",
            "link",
        },
    )

    for value in values:

        if _is_http_url(
            value
        ):

            return value

    raise SpotifyDownloadError(
        "Spotify Downloader9 did not "
        "return a usable downloadLink."
    )


# ============================================================
# FILE HELPERS
# ============================================================


def _safe_filename(
    value: str,
) -> str:

    value = _text(
        value
    )

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
            urlparse(
                url
            ).path
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
        .split(
            ";",
            1,
        )[0]
        .strip()
        .lower()
    )

    mapping = {
        "audio/mpeg":
            ".mp3",

        "audio/mp3":
            ".mp3",

        "audio/mp4":
            ".m4a",

        "audio/x-m4a":
            ".m4a",

        "audio/aac":
            ".aac",

        "audio/ogg":
            ".ogg",

        "audio/opus":
            ".opus",

        "audio/wav":
            ".wav",

        "audio/x-wav":
            ".wav",

        "audio/flac":
            ".flac",

        "audio/webm":
            ".webm",
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
        / (
            "spotify_"
            f"{track_id}_"
            f"{int(time.time() * 1000)}"
        )
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return job_dir


# ============================================================
# AUDIO DOWNLOAD
# ============================================================


def _download_audio_file(
    url: str,
    destination: Path,
) -> str:

    temporary = destination.with_name(
        destination.name
        + ".part"
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

            content_length = (
                response.headers.get(
                    "Content-Length",
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

            if content_length:

                print(
                    "[Spotify] Content-Length:",
                    content_length,
                )

            with open(
                temporary,
                "wb",
            ) as file:

                for chunk in (
                    response.iter_content(
                        chunk_size=
                        1024 * 1024
                    )
                ):

                    if chunk:

                        file.write(
                            chunk
                        )

        if not temporary.exists():

            raise SpotifyDownloadError(
                "Downloaded audio file "
                "was not created."
            )

        file_size = (
            temporary.stat()
            .st_size
        )

        if file_size <= 0:

            raise SpotifyDownloadError(
                "Downloaded Spotify "
                "audio is empty."
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
            "Spotify audio request failed: "
            f"{exc}"
        ) from exc


# ============================================================
# MAIN SPOTIFY DOWNLOAD
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
        "[Spotify] Downloading track:",
        normalized_id,
    )

    # --------------------------------------------------------
    # FIRST API
    # Spotify Downloader9
    # --------------------------------------------------------

    response = (
        _request_downloader(
            normalized_id
        )
    )

    downloader_data = (
        _extract_downloader_data(
            response
        )
    )

    if not downloader_data:

        raise SpotifyDownloadError(
            "Spotify Downloader9 returned "
            "no track data."
        )

    # --------------------------------------------------------
    # EXACT API FIELDS
    # --------------------------------------------------------

    title = _text(
        downloader_data.get(
            "title"
        )
    )

    artist = _text(
        downloader_data.get(
            "artist"
        )
    )

    album = _text(
        downloader_data.get(
            "album"
        )
    )

    artwork = (
        downloader_data.get(
            "cover"
        )
    )

    duration = (
        downloader_data.get(
            "duration"
        )
    )

    quality = (
        downloader_data.get(
            "quality"
        )
        or downloader_data.get(
            "bitrate"
        )
        or downloader_data.get(
            "bitRate"
        )
    )

    download_url = (
        _extract_download_url(
            downloader_data
        )
    )

    # --------------------------------------------------------
    # FALLBACK METADATA FROM API40
    # --------------------------------------------------------

    if (
        not title
        or not artist
        or not album
        or not artwork
    ):

        metadata = (
            get_spotify_track(
                normalized_id
            )
        )

        if metadata:

            if not title:

                title = metadata.get(
                    "title"
                )

            if not artist:

                artist = metadata.get(
                    "artist"
                )

            if not album:

                album = metadata.get(
                    "album"
                )

            if not artwork:

                artwork = metadata.get(
                    "artwork"
                )

            if duration is None:

                duration = metadata.get(
                    "duration"
                )

    # --------------------------------------------------------
    # FINAL METADATA SAFETY
    # --------------------------------------------------------

    title = (
        title
        or "Unknown Track"
    )

    artist = (
        artist
        or "Unknown Artist"
    )

    album = (
        album
        or ""
    )

    # --------------------------------------------------------
    # DURATION NORMALIZATION
    # --------------------------------------------------------

    if duration is not None:

        try:

            duration = float(
                duration
            )

            if duration > 10000:

                duration /= 1000

            duration = int(
                duration
            )

        except (
            TypeError,
            ValueError,
        ):

            duration = None

    # --------------------------------------------------------
    # QUALITY
    # --------------------------------------------------------

    quality = _text(
        quality
    )

    if not quality:

        quality = "Spotify Audio"

    # --------------------------------------------------------
    # JOB DIRECTORY
    # --------------------------------------------------------

    job_dir = _create_job_dir(
        output_dir,
        normalized_id,
    )

    # --------------------------------------------------------
    # FILENAME
    # --------------------------------------------------------

    base_name = _safe_filename(
        f"{artist} - {title}"
    )

    extension = (
        _extension_from_url(
            download_url
        )
    )

    if not extension:

        extension = ".mp3"

    output_path = (
        job_dir
        / f"{base_name}{extension}"
    )

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    content_type = (
        _download_audio_file(
            download_url,
            output_path,
        )
    )

    # --------------------------------------------------------
    # CORRECT EXTENSION
    # --------------------------------------------------------

    if (
        output_path.suffix
        == ".mp3"
        and content_type
    ):

        detected = (
            _extension_from_content_type(
                content_type
            )
        )

        # Do not rename an actual MP3.
        if (
            detected
            and detected != ".mp3"
            and output_path.exists()
        ):

            corrected = (
                job_dir
                / (
                    f"{base_name}"
                    f"{detected}"
                )
            )

            output_path.replace(
                corrected
            )

            output_path = corrected

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    if not output_path.exists():

        raise SpotifyDownloadError(
            "Spotify audio file "
            "does not exist."
        )

    file_size = (
        output_path.stat()
        .st_size
    )

    if file_size <= 0:

        raise SpotifyDownloadError(
            "Spotify audio file is empty."
        )

    print(
        "[Spotify] Download complete."
    )

    print(
        "[Spotify] Title:",
        title,
    )

    print(
        "[Spotify] Artist:",
        artist,
    )

    print(
        "[Spotify] Album:",
        album,
    )

    print(
        "[Spotify] Size:",
        file_size,
        "bytes",
    )

    return {
        "path":
            str(output_path),

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
            artwork,

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
# CONFIGURATION
# ============================================================


def spotify_configured() -> bool:

    return bool(
        SPOTIFY_API40_KEY
        and SPOTIFY_API40_HOST
        and RAPIDAPI_KEY
        and RAPIDAPI_HOST
    )


# ============================================================
# DIAGNOSTIC
# ============================================================


if __name__ == "__main__":

    print(
        "======================================"
    )

    print(
        " Audio Bot Spotify Configuration"
    )

    print(
        "======================================"
    )

    print(
        "SPOTIFY_API40_HOST:",
        SPOTIFY_API40_HOST,
    )

    print(
        "SPOTIFY_API40_KEY:",
        "CONFIGURED"
        if SPOTIFY_API40_KEY
        else "MISSING",
    )

    print(
        "RAPIDAPI_HOST:",
        RAPIDAPI_HOST,
    )

    print(
        "RAPIDAPI_KEY:",
        "CONFIGURED"
        if RAPIDAPI_KEY
        else "MISSING",
    )

    print(
        "Configuration:",
        "OK"
        if spotify_configured()
        else "INCOMPLETE",
    )

    print(
        "======================================"
    )