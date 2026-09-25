```python
"""
Spotify integration for Audio Bot.

Supported text searches:

    Apna Bana Le spotify
    Apna Bana Le - spotify
    spotify Apna Bana Le
    spotify - Apna Bana Le

Supported Spotify URL types:

    track
    album
    playlist
    artist

This module uses RapidAPI for Spotify metadata/search only.

IMPORTANT:
This module does NOT download Spotify audio.

Audio flow remains:

    Spotify metadata
        ->
    YouTube search
        ->
    yt-dlp
        ->
    FFmpeg
        ->
    Telegram

Environment variables:

    RAPIDAPI_KEY
    RAPIDAPI_HOST
    SPOTIFY_SEARCH_LIMIT
    SPOTIFY_TIMEOUT
"""

from __future__ import annotations

import html
import logging
import os
import re
from typing import Any

import requests


logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

RAPIDAPI_KEY = os.getenv(
    "RAPIDAPI_KEY",
    "",
).strip()

RAPIDAPI_HOST = os.getenv(
    "RAPIDAPI_HOST",
    "spotify-downloader9.p.rapidapi.com",
).strip()

SPOTIFY_SEARCH_URL = (
    f"https://{RAPIDAPI_HOST}/search"
)

SPOTIFY_TIMEOUT = max(
    5,
    int(
        os.getenv(
            "SPOTIFY_TIMEOUT",
            "20",
        )
    ),
)

SPOTIFY_SEARCH_LIMIT = max(
    1,
    min(
        10,
        int(
            os.getenv(
                "SPOTIFY_SEARCH_LIMIT",
                "8",
            )
        ),
    ),
)

SPOTIFY_MAX_RESULTS = 20


# ============================================================
# REGEX
# ============================================================

SPOTIFY_URL_PATTERN = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?"
    r"(track|album|playlist|artist)/"
    r"([A-Za-z0-9]+)"
    r"(?:\?[^\s]*)?",
    re.IGNORECASE,
)

SPOTIFY_TRACK_URL_PATTERN = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?track/"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)

SPOTIFY_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9]{22}$"
)


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(
    value: Any,
    default: str = "",
) -> str:

    if value is None:
        return default

    if isinstance(
        value,
        str,
    ):
        return value.strip()

    return str(value).strip()


def escape_html(
    value: Any,
) -> str:

    return html.escape(
        clean_text(value)
    )


def normalize_key(
    value: Any,
) -> str:

    text = clean_text(
        value
    ).lower()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# ============================================================
# SPOTIFY URL DETECTION
# ============================================================

def is_spotify_url(
    text: str,
) -> bool:

    if not text:
        return False

    return bool(
        SPOTIFY_URL_PATTERN.search(
            text.strip()
        )
    )


def parse_spotify_url(
    url: str,
) -> tuple[str, str] | None:

    if not url:
        return None

    match = SPOTIFY_URL_PATTERN.search(
        url.strip()
    )

    if not match:
        return None

    resource_type = (
        match.group(1)
        .lower()
    )

    resource_id = (
        match.group(2)
    )

    return (
        resource_type,
        resource_id,
    )


def extract_spotify_track_id(
    url: str,
) -> str | None:

    if not url:
        return None

    match = SPOTIFY_TRACK_URL_PATTERN.search(
        url.strip()
    )

    if not match:
        return None

    spotify_id = (
        match.group(1)
    )

    if not SPOTIFY_ID_PATTERN.fullmatch(
        spotify_id
    ):
        return None

    return spotify_id


def build_spotify_url(
    spotify_id: str,
) -> str:

    return (
        "https://open.spotify.com/track/"
        f"{spotify_id}"
    )


def build_spotify_resource_url(
    resource_type: str,
    resource_id: str,
) -> str:

    return (
        "https://open.spotify.com/"
        f"{resource_type}/"
        f"{resource_id}"
    )


# ============================================================
# SPOTIFY SEARCH DETECTION
# ============================================================

def is_spotify_search(
    text: str,
) -> bool:
    """
    Supported:

        Apna Bana Le spotify
        Apna Bana Le - spotify
        spotify Apna Bana Le
        spotify - Apna Bana Le

    Direct Spotify URLs are also recognized.
    """

    if not text:
        return False

    value = text.strip()

    if not value:
        return False

    # Direct Spotify URL.
    if is_spotify_url(
        value
    ):
        return True

    # song spotify
    if re.search(
        r"(?:^|\s)spotify\s*$",
        value,
        re.IGNORECASE,
    ):
        return True

    # song - spotify
    if re.search(
        r"\s*-\s*spotify\s*$",
        value,
        re.IGNORECASE,
    ):
        return True

    # spotify song
    if re.match(
        r"^\s*spotify(?:\s*-\s*|\s+).+",
        value,
        re.IGNORECASE,
    ):
        return True

    return False


def clean_spotify_query(
    text: str,
) -> str:
    """
    Remove the Spotify trigger.

    Examples:

        Apna Bana Le spotify
        -> Apna Bana Le

        Apna Bana Le - spotify
        -> Apna Bana Le

        spotify Apna Bana Le
        -> Apna Bana Le

        spotify - Apna Bana Le
        -> Apna Bana Le
    """

    if not text:
        return ""

    query = text.strip()

    # Direct URL remains untouched.
    if is_spotify_url(
        query
    ):
        return query

    query = re.sub(
        r"^\s*spotify\s*-\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )

    query = re.sub(
        r"^\s*spotify\s+",
        "",
        query,
        count=1,
        flags=re.IGNORECASE,
    )

    query = re.sub(
        r"\s*-\s*spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )

    query = re.sub(
        r"\s+spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )

    return query.strip()


# ============================================================
# ARTIST EXTRACTION
# ============================================================

def extract_artists(
    item: Any,
) -> list[str]:
    """
    Extract artist names from multiple possible
    RapidAPI/Spotify response structures.
    """

    if not isinstance(
        item,
        dict,
    ):
        return []

    result: list[str] = []

    candidates = [
        item.get("artists"),
        item.get("artist"),
        item.get("artistName"),
        item.get("artist_name"),
    ]

    # --------------------------------------------------------
    # Direct artists
    # --------------------------------------------------------

    for artists in candidates:

        if not artists:
            continue

        if isinstance(
            artists,
            list,
        ):

            for artist in artists:

                if isinstance(
                    artist,
                    str,
                ):

                    name = artist.strip()

                elif isinstance(
                    artist,
                    dict,
                ):

                    profile = artist.get(
                        "profile"
                    )

                    profile_name = ""

                    if isinstance(
                        profile,
                        dict,
                    ):

                        profile_name = clean_text(
                            profile.get("name")
                        )

                    name = clean_text(
                        artist.get("name")
                        or artist.get("title")
                        or artist.get("artist")
                        or profile_name
                    )

                else:
                    continue

                if name:
                    result.append(
                        name
                    )

        elif isinstance(
            artists,
            dict,
        ):

            profile = artists.get(
                "profile"
            )

            profile_name = ""

            if isinstance(
                profile,
                dict,
            ):

                profile_name = clean_text(
                    profile.get("name")
                )

            name = clean_text(
                artists.get("name")
                or artists.get("title")
                or artists.get("artist")
                or profile_name
            )

            if name:
                result.append(
                    name
                )

        elif isinstance(
            artists,
            str,
        ):

            for artist in artists.split(
                ","
            ):

                name = artist.strip()

                if name:
                    result.append(
                        name
                    )

    # --------------------------------------------------------
    # Nested Spotify-style artist data
    # --------------------------------------------------------

    nested_candidates = [
        item.get("artist"),
        item.get("artists"),
    ]

    for candidate in nested_candidates:

        if not isinstance(
            candidate,
            dict,
        ):
            continue

        profile = candidate.get(
            "profile"
        )

        if isinstance(
            profile,
            dict,
        ):

            name = clean_text(
                profile.get("name")
            )

            if name:
                result.append(
                    name
                )

        name = clean_text(
            candidate.get("name")
        )

        if name:
            result.append(
                name
            )

    # --------------------------------------------------------
    # Remove duplicate artist names
    # --------------------------------------------------------

    unique: list[str] = []
    seen: set[str] = set()

    for artist in result:

        key = normalize_key(
            artist
        )

        if not key:
            continue

        if key in seen:
            continue

        seen.add(
            key
        )

        unique.append(
            artist
        )

    return unique


def extract_artist(
    item: dict[str, Any],
) -> str:

    artists = extract_artists(
        item
    )

    if artists:
        return ", ".join(
            artists
        )

    for key in (
        "artist",
        "artistName",
        "artist_name",
        "performer",
        "singer",
    ):

        value = clean_text(
            item.get(key)
        )

        if value:
            return value

    album = item.get(
        "album"
    )

    if isinstance(
        album,
        dict,
    ):

        artists = extract_artists(
            album
        )

        if artists:
            return ", ".join(
                artists
            )

    return "Unknown Artist"


# ============================================================
# COVER EXTRACTION
# ============================================================

def extract_cover(
    item: Any,
) -> str | None:

    if not isinstance(
        item,
        dict,
    ):
        return None

    for key in (
        "cover",
        "thumbnail",
        "image",
        "image_url",
        "cover_url",
        "thumbnailUrl",
        "thumbnail_url",
    ):

        value = item.get(
            key
        )

        if isinstance(
            value,
            str,
        ) and value.strip():

            return value.strip()

    images = item.get(
        "images"
    )

    if isinstance(
        images,
        list,
    ):

        for image in images:

            if isinstance(
                image,
                str,
            ) and image.strip():

                return image.strip()

            if not isinstance(
                image,
                dict,
            ):
                continue

            url = clean_text(
                image.get("url")
            )

            if url:
                return url

    cover_art = item.get(
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

            for source in reversed(
                sources
            ):

                if not isinstance(
                    source,
                    dict,
                ):
                    continue

                url = clean_text(
                    source.get("url")
                )

                if url:
                    return url

    album = item.get(
        "album"
    )

    if isinstance(
        album,
        dict,
    ):

        cover = extract_cover(
            album
        )

        if cover:
            return cover

    return None


# ============================================================
# DURATION
# ============================================================

def format_duration(
    value: Any,
) -> str:

    if value is None:
        return ""

    if isinstance(
        value,
        dict,
    ):

        value = (
            value.get(
                "totalMilliseconds"
            )
            or value.get(
                "milliseconds"
            )
            or value.get(
                "duration_ms"
            )
        )

    if isinstance(
        value,
        str,
    ):

        value = value.strip()

        if not value:
            return ""

        if ":" in value:
            return value

    try:

        number = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return ""

    if number > 10000:

        total_seconds = int(
            number / 1000
        )

    else:

        total_seconds = int(
            number
        )

    if total_seconds <= 0:
        return ""

    minutes, seconds = divmod(
        total_seconds,
        60,
    )

    hours, minutes = divmod(
        minutes,
        60,
    )

    if hours:

        return (
            f"{hours}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return (
        f"{minutes}:"
        f"{seconds:02d}"
    )


# ============================================================
# TITLE EXTRACTION
# ============================================================

def extract_title(
    item: dict[str, Any],
) -> str:

    for key in (
        "title",
        "name",
        "trackName",
        "track_name",
    ):

        value = clean_text(
            item.get(key)
        )

        if value:
            return value

    nested_item = item.get(
        "item"
    )

    if isinstance(
        nested_item,
        dict,
    ):

        title = extract_title(
            nested_item
        )

        if title:
            return title

    item_v2 = item.get(
        "itemV2"
    )

    if isinstance(
        item_v2,
        dict,
    ):

        title = extract_title(
            item_v2
        )

        if title:
            return title

    return "Unknown Track"


# ============================================================
# ALBUM EXTRACTION
# ============================================================

def extract_album(
    item: dict[str, Any],
) -> str:

    album = item.get(
        "album"
    )

    if isinstance(
        album,
        str,
    ):

        return album.strip()

    if isinstance(
        album,
        dict,
    ):

        return clean_text(
            album.get("name")
            or album.get("title")
        )

    for key in (
        "albumName",
        "album_name",
    ):

        value = clean_text(
            item.get(key)
        )

        if value:
            return value

    return "Unknown Album"


# ============================================================
# SPOTIFY ID EXTRACTION
# ============================================================

def extract_track_id(
    item: dict[str, Any],
) -> str:

    for key in (
        "id",
        "track_id",
        "spotify_id",
    ):

        value = clean_text(
            item.get(key)
        )

        if value:
            return value

    uri = clean_text(
        item.get("uri")
    )

    if uri.startswith(
        "spotify:track:"
    ):

        return uri.split(
            "spotify:track:",
            1,
        )[1].strip()

    nested = item.get(
        "item"
    )

    if isinstance(
        nested,
        dict,
    ):

        value = extract_track_id(
            nested
        )

        if value:
            return value

    return ""


# ============================================================
# TRACK NORMALIZATION
# ============================================================

def normalize_track(
    item: dict[str, Any],
) -> dict[str, Any]:

    if not isinstance(
        item,
        dict,
    ):
        return {}

    spotify_id = extract_track_id(
        item
    )

    title = extract_title(
        item
    )

    artist = extract_artist(
        item
    )

    album = extract_album(
        item
    )

    duration = (
        item.get("duration")
        or item.get("duration_ms")
        or item.get("length")
    )

    if not duration:

        duration_data = item.get(
            "trackDuration"
        )

        if isinstance(
            duration_data,
            dict,
        ):

            duration = duration_data

    external_urls = item.get(
        "external_urls"
    )

    spotify_url = ""

    if isinstance(
        external_urls,
        dict,
    ):

        spotify_url = clean_text(
            external_urls.get(
                "spotify"
            )
        )

    if (
        not spotify_url
        and spotify_id
    ):

        spotify_url = build_spotify_url(
            spotify_id
        )

    return {
        "id": spotify_id,
        "title": title,
        "artist": artist,
        "album": album,
        "cover": extract_cover(
            item
        ),
        "duration": format_duration(
            duration
        ),
        "spotify_url": spotify_url,
        "raw": item,
    }


# ============================================================
# RESULT DEDUPLICATION
# ============================================================

def deduplicate_tracks(
    tracks: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    unique: list[
        dict[str, Any]
    ] = []

    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()

    for track in tracks:

        if not track:
            continue

        title = clean_text(
            track.get("title")
        )

        artist = clean_text(
            track.get("artist")
        )

        if not title:
            continue

        spotify_id = clean_text(
            track.get("id")
        )

        if spotify_id:

            id_key = normalize_key(
                spotify_id
            )

            if id_key in seen_ids:
                continue

            seen_ids.add(
                id_key
            )

        else:

            fingerprint = "|".join(
                [
                    normalize_key(
                        title
                    ),
                    normalize_key(
                        artist
                    ),
                    normalize_key(
                        track.get(
                            "duration"
                        )
                    ),
                ]
            )

            if fingerprint in seen_fingerprints:
                continue

            seen_fingerprints.add(
                fingerprint
            )

        unique.append(
            track
        )

    return unique


# ============================================================
# API RESPONSE EXTRACTION
# ============================================================

def find_track_items(
    payload: Any,
) -> list[dict[str, Any]]:

    if not isinstance(
        payload,
        dict,
    ):
        return []

    candidates: list[Any] = [
        payload,
        payload.get("data"),
        payload.get("result"),
        payload.get("results"),
    ]

    for candidate in candidates:

        if not isinstance(
            candidate,
            dict,
        ):
            continue

        tracks = candidate.get(
            "tracks"
        )

        if isinstance(
            tracks,
            list,
        ):

            return [
                item
                for item in tracks
                if isinstance(
                    item,
                    dict,
                )
            ]

        if isinstance(
            tracks,
            dict,
        ):

            items = tracks.get(
                "items"
            )

            if isinstance(
                items,
                list,
            ):

                return [
                    item
                    for item in items
                    if isinstance(
                        item,
                        dict,
                    )
                ]

        items = candidate.get(
            "items"
        )

        if isinstance(
            items,
            list,
        ):

            valid_items = [
                item
                for item in items
                if isinstance(
                    item,
                    dict,
                )
            ]

            if valid_items:
                return valid_items

        songs = candidate.get(
            "songs"
        )

        if isinstance(
            songs,
            list,
        ):

            return [
                item
                for item in songs
                if isinstance(
                    item,
                    dict,
                )
            ]

    return []


# ============================================================
# RAPIDAPI REQUEST
# ============================================================

def search_tracks(
    query: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:

    if not RAPIDAPI_KEY:

        raise RuntimeError(
            "RAPIDAPI_KEY is not configured."
        )

    if not RAPIDAPI_HOST:

        raise RuntimeError(
            "RAPIDAPI_HOST is not configured."
        )

    query = clean_text(
        query
    )

    if not query:
        return []

    if limit is None:
        limit = SPOTIFY_SEARCH_LIMIT

    limit = max(
        1,
        min(
            int(limit),
            SPOTIFY_MAX_RESULTS,
        ),
    )

    params = {
        "q": query,
        "type": "tracks",
        "limit": limit,
    }

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }

    logger.info(
        "Spotify search query: %s",
        query,
    )

    try:

        response = requests.get(
            SPOTIFY_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=SPOTIFY_TIMEOUT,
        )

    except requests.Timeout as exc:

        raise RuntimeError(
            "Spotify search timed out. "
            "Please try again."
        ) from exc

    except requests.RequestException as exc:

        logger.exception(
            "Spotify API request failed."
        )

        raise RuntimeError(
            "Unable to connect to the Spotify Search API."
        ) from exc

    # ========================================================
    # HTTP STATUS
    # ========================================================

    if response.status_code == 429:

        retry_after = response.headers.get(
            "Retry-After"
        )

        if retry_after:

            raise RuntimeError(
                "Spotify API rate limit reached. "
                f"Retry after {retry_after} seconds."
            )

        raise RuntimeError(
            "Spotify API rate limit reached. "
            "Please try again later."
        )

    if response.status_code in (
        401,
        403,
    ):

        raise RuntimeError(
            "RapidAPI authentication failed. "
            "Check RAPIDAPI_KEY and the API subscription."
        )

    if response.status_code == 404:

        raise RuntimeError(
            "Spotify Search endpoint was not found. "
            "Check RAPIDAPI_HOST."
        )

    if not response.ok:

        body = response.text.strip()

        if len(body) > 500:
            body = body[:500]

        raise RuntimeError(
            "Spotify API returned HTTP "
            f"{response.status_code}: {body}"
        )

    # ========================================================
    # JSON
    # ========================================================

    try:

        payload = response.json()

    except ValueError as exc:

        raise RuntimeError(
            "Spotify API returned invalid JSON."
        ) from exc

    # ========================================================
    # API SUCCESS FLAG
    # ========================================================

    if isinstance(
        payload,
        dict,
    ):

        if payload.get(
            "success"
        ) is False:

            error = payload.get(
                "error"
            )

            if isinstance(
                error,
                dict,
            ):

                error = (
                    error.get("message")
                    or error.get("detail")
                    or str(error)
                )

            raise RuntimeError(
                clean_text(
                    error,
                    "Spotify API request failed.",
                )
            )

    # ========================================================
    # EXTRACT RESULTS
    # ========================================================

    items = find_track_items(
        payload
    )

    if not items:
        return []

    results: list[
        dict[str, Any]
    ] = []

    for item in items:

        track = normalize_track(
            item
        )

        title = clean_text(
            track.get("title")
        )

        if (
            not title
            or title == "Unknown Track"
        ):
            continue

        results.append(
            track
        )

        if len(results) >= limit:
            break

    results = deduplicate_tracks(
        results
    )

    return results[:limit]


# ============================================================
# PLAYLIST METADATA HELPERS
# ============================================================

def extract_playlist_items(
    payload: Any,
) -> list[dict[str, Any]]:

    if not isinstance(
        payload,
        dict,
    ):
        return []

    candidates: list[Any] = [
        payload,
        payload.get("data"),
        payload.get("result"),
        payload.get("results"),
    ]

    for candidate in candidates:

        if not isinstance(
            candidate,
            dict,
        ):
            continue

        items = candidate.get(
            "items"
        )

        if isinstance(
            items,
            list,
        ):

            return [
                item
                for item in items
                if isinstance(
                    item,
                    dict,
                )
            ]

        tracks = candidate.get(
            "tracks"
        )

        if isinstance(
            tracks,
            list,
        ):

            return [
                item
                for item in tracks
                if isinstance(
                    item,
                    dict,
                )
            ]

        if isinstance(
            tracks,
            dict,
        ):

            nested_items = tracks.get(
                "items"
            )

            if isinstance(
                nested_items,
                list,
            ):

                return [
                    item
                    for item in nested_items
                    if isinstance(
                        item,
                        dict,
                    )
                ]

    return []


def normalize_playlist_item(
    item: dict[str, Any],
) -> dict[str, Any]:

    track = item

    nested = (
        item.get("track")
        or item.get("item")
    )

    if isinstance(
        nested,
        dict,
    ):

        track = nested

    return normalize_track(
        track
    )


def normalize_playlist_tracks(
    items: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:

    if limit is None:

        limit = max(
            1,
            SPOTIFY_SEARCH_LIMIT,
        )

    tracks: list[
        dict[str, Any]
    ] = []

    for item in items:

        track = normalize_playlist_item(
            item
        )

        title = clean_text(
            track.get("title")
        )

        if (
            not title
            or title == "Unknown Track"
        ):
            continue

        tracks.append(
            track
        )

    tracks = deduplicate_tracks(
        tracks
    )

    return tracks[:limit]


# ============================================================
# MAIN SPOTIFY SEARCH FUNCTION
# ============================================================

def search_spotify(
    user_text: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    Process a Spotify request.

    Text search:

        Apna Bana Le spotify

    Direct URL:

        https://open.spotify.com/track/...

        https://open.spotify.com/playlist/...

    The function returns the resource type and ID
    for direct Spotify URLs so bot.py can handle
    the appropriate resource.
    """

    if not is_spotify_search(
        user_text
    ):

        return {
            "is_spotify": False,
            "is_url": False,
            "resource_type": None,
            "resource_id": None,
            "query": clean_text(
                user_text
            ),
            "results": [],
        }

    query = clean_spotify_query(
        user_text
    )

    # ========================================================
    # DIRECT SPOTIFY URL
    # ========================================================

    if is_spotify_url(
        query
    ):

        resource = parse_spotify_url(
            query
        )

        resource_type = (
            resource[0]
            if resource
            else None
        )

        resource_id = (
            resource[1]
            if resource
            else None
        )

        return {
            "is_spotify": True,
            "is_url": True,
            "query": "",
            "spotify_url": query,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "track_id": (
                extract_spotify_track_id(
                    query
                )
            ),
            "results": [],
        }

    # ========================================================
    # EMPTY QUERY
    # ========================================================

    if not query:

        return {
            "is_spotify": True,
            "is_url": False,
            "resource_type": None,
            "resource_id": None,
            "query": "",
            "results": [],
        }

    # ========================================================
    # TEXT SEARCH
    # ========================================================

    results = search_tracks(
        query,
        limit=limit,
    )

    return {
        "is_spotify": True,
        "is_url": False,
        "resource_type": None,
        "resource_id": None,
        "query": query,
        "results": results,
    }


# ============================================================
# TELEGRAM FORMATTING
# ============================================================

def format_result(
    track: dict[str, Any],
    number: int,
) -> str:

    title = escape_html(
        track.get("title")
    )

    artist = escape_html(
        track.get("artist")
    )

    album = escape_html(
        track.get("album")
    )

    duration = escape_html(
        track.get("duration")
    )

    lines = [
        f"<b>{number}. {title}</b>",
        f"👤 {artist}",
    ]

    if (
        album
        and album != "Unknown Album"
    ):

        lines.append(
            f"💿 {album}"
        )

    if duration:

        lines.append(
            f"⏱ {duration}"
        )

    return "\n".join(
        lines
    )


def format_search_results(
    query: str,
    results: list[dict[str, Any]],
) -> str:

    safe_query = escape_html(
        query
    )

    if not results:

        return (
            "🔎 <b>Spotify Search</b>\n\n"
            f"Query: <code>{safe_query}</code>\n\n"
            "❌ No matching songs found."
        )

    lines = [
        "🎵 <b>Spotify Search</b>",
        "",
        f"🔎 <code>{safe_query}</code>",
        "",
        "Select a song:",
        "",
    ]

    for index, track in enumerate(
        results,
        start=1,
    ):

        lines.append(
            format_result(
                track,
                index,
            )
        )

        if index != len(
            results
        ):

            lines.append(
                ""
            )

    return "\n".join(
        lines
    )


# ============================================================
# STATUS
# ============================================================

def spotify_status() -> dict[str, Any]:

    return {
        "configured": bool(
            RAPIDAPI_KEY
        ),
        "host": RAPIDAPI_HOST,
        "search_url": SPOTIFY_SEARCH_URL,
        "search_type": "tracks",
        "search_limit": SPOTIFY_SEARCH_LIMIT,
        "audio_download": False,
        "spotify_url_types": [
            "track",
            "album",
            "playlist",
            "artist",
        ],
    }


# ============================================================
# USER-FRIENDLY ERRORS
# ============================================================

def friendly_error(
    error: Exception,
) -> str:

    message = clean_text(
        error
    )

    lowered = message.lower()

    if (
        "rate limit" in lowered
        or "429" in lowered
    ):

        return (
            "⏳ <b>Spotify search is temporarily "
            "rate limited.</b>\n\n"
            "Please try again shortly."
        )

    if (
        "authentication" in lowered
        or "rapidapi_key" in lowered
        or "401" in lowered
        or "403" in lowered
    ):

        return (
            "❌ <b>Spotify API authentication failed.</b>\n\n"
            "Check the RapidAPI key and subscription."
        )

    if (
        "not found" in lowered
        or "404" in lowered
    ):

        return (
            "❌ <b>Spotify Search API was not found.</b>\n\n"
            "Check the configured RapidAPI host."
        )

    if "timed out" in lowered:

        return (
            "⏱️ <b>Spotify search timed out.</b>\n\n"
            "Please try again."
        )

    return (
        "❌ <b>Spotify search failed.</b>\n\n"
        f"<code>{escape_html(message[:400])}</code>"
    )
```
