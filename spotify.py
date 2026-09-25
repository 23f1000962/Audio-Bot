"""
Spotify search integration for Audio Bot.

Spotify mode is explicitly triggered by:
    <song name> spotify
    <song name> - spotify
    spotify <song name>
    spotify - <song name>

Direct Spotify track URLs are also recognized.

This module uses the RapidAPI Spotify Search endpoint.

RapidAPI host:
    spotify-downloader9.p.rapidapi.com

Search parameters:
    q
    type=tracks
    limit

Environment variables:
    RAPIDAPI_KEY
    RAPIDAPI_HOST
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

SPOTIFY_TIMEOUT = int(
    os.getenv(
        "SPOTIFY_TIMEOUT",
        "20",
    )
)

# Number of Spotify results returned to the bot.
SPOTIFY_SEARCH_LIMIT = int(
    os.getenv(
        "SPOTIFY_SEARCH_LIMIT",
        "8",
    )
)

# Maximum allowed results.
SPOTIFY_MAX_RESULTS = 20


# ============================================================
# REGEX
# ============================================================

SPOTIFY_URL_PATTERN = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(track|album|playlist|artist)/"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)

SPOTIFY_TRACK_URL_PATTERN = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"track/"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)

SPOTIFY_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9]{22}$"
)


# ============================================================
# SPOTIFY COMMAND DETECTION
# ============================================================

def is_spotify_url(
    text: str,
) -> bool:
    """
    Check whether text contains a Spotify URL.
    """

    if not text:
        return False

    return bool(
        SPOTIFY_URL_PATTERN.search(
            text.strip()
        )
    )


def is_spotify_search(
    text: str,
) -> bool:
    """
    Determine whether the user explicitly requested
    Spotify search.

    Supported forms:

        Apna Bana Le spotify
        Apna Bana Le - spotify
        spotify Apna Bana Le
        spotify - Apna Bana Le

    Case-insensitive.
    """

    if not text:
        return False

    value = text.strip()

    if not value:
        return False

    # Direct Spotify URL.
    if is_spotify_url(value):
        return True

    # "song spotify"
    if re.search(
        r"(?:^|\s|-)spotify\s*$",
        value,
        re.IGNORECASE,
    ):
        return True

    # "song - spotify"
    if re.search(
        r"\s*-\s*spotify\s*$",
        value,
        re.IGNORECASE,
    ):
        return True

    # "spotify song"
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
    Remove the Spotify trigger from a user query.

    Examples:

        "Apna Bana Le spotify"
            -> "Apna Bana Le"

        "Apna Bana Le - spotify"
            -> "Apna Bana Le"

        "spotify Apna Bana Le"
            -> "Apna Bana Le"

        "spotify - Apna Bana Le"
            -> "Apna Bana Le"
    """

    if not text:
        return ""

    query = text.strip()

    # Remove Spotify URL handling separately.
    if is_spotify_url(query):
        return query

    # --------------------------------------------------------
    # "spotify - song"
    # --------------------------------------------------------

    query = re.sub(
        r"^\s*spotify\s*-\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )

    # --------------------------------------------------------
    # "spotify song"
    # --------------------------------------------------------

    if re.match(
        r"^\s*spotify\s+",
        query,
        re.IGNORECASE,
    ):
        query = re.sub(
            r"^\s*spotify\s+",
            "",
            query,
            count=1,
            flags=re.IGNORECASE,
        )

    # --------------------------------------------------------
    # "song - spotify"
    # --------------------------------------------------------

    query = re.sub(
        r"\s*-\s*spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )

    # --------------------------------------------------------
    # "song spotify"
    # --------------------------------------------------------

    query = re.sub(
        r"\s+spotify\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )

    return query.strip()


# ============================================================
# URL HELPERS
# ============================================================

def parse_spotify_url(
    url: str,
) -> tuple[str, str] | None:
    """
    Extract Spotify resource type and ID.

    Example:

        https://open.spotify.com/track/7jT3LcNj4XPYOlbNkPWNhU

    Returns:

        ("track", "7jT3LcNj4XPYOlbNkPWNhU")
    """

    if not url:
        return None

    match = SPOTIFY_URL_PATTERN.search(
        url.strip()
    )

    if not match:
        return None

    return (
        match.group(1).lower(),
        match.group(2),
    )


def extract_spotify_track_id(
    url: str,
) -> str | None:
    """
    Extract a Spotify track ID from a track URL.
    """

    if not url:
        return None

    match = SPOTIFY_TRACK_URL_PATTERN.search(
        url.strip()
    )

    if not match:
        return None

    spotify_id = match.group(1)

    if not SPOTIFY_ID_PATTERN.fullmatch(
        spotify_id
    ):
        return None

    return spotify_id


def build_spotify_url(
    spotify_id: str,
) -> str:
    """
    Build a Spotify track URL.
    """

    return (
        "https://open.spotify.com/track/"
        f"{spotify_id}"
    )


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(
    value: Any,
    default: str = "",
) -> str:
    """
    Safely convert a value into a clean string.
    """

    if value is None:
        return default

    return str(value).strip()


def escape_html(
    value: Any,
) -> str:
    """
    Escape text before displaying it in Telegram HTML.
    """

    return html.escape(
        clean_text(value)
    )


# ============================================================
# ARTIST EXTRACTION
# ============================================================

def extract_artists(
    item: dict[str, Any],
) -> list[str]:
    """
    Extract artist names from possible API formats.
    """

    artists = item.get("artists")

    if not artists:
        return []

    result: list[str] = []

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
                name = clean_text(
                    artist.get("name")
                    or artist.get("title")
                )

            else:
                continue

            if name:
                result.append(name)

    elif isinstance(
        artists,
        str,
    ):
        result = [
            artist.strip()
            for artist in artists.split(",")
            if artist.strip()
        ]

    return result


def extract_artist(
    item: dict[str, Any],
) -> str:
    """
    Extract a formatted artist string.
    """

    artists = extract_artists(
        item
    )

    if artists:
        return ", ".join(
            artists
        )

    return clean_text(
        item.get("artist"),
        "Unknown Artist",
    )


# ============================================================
# COVER EXTRACTION
# ============================================================

def extract_cover(
    item: dict[str, Any],
) -> str | None:
    """
    Extract artwork URL from possible API formats.
    """

    # Direct cover field.
    cover = item.get("cover")

    if isinstance(
        cover,
        str,
    ) and cover.strip():

        return cover.strip()

    # Other common fields.
    for key in (
        "thumbnail",
        "image",
        "image_url",
        "cover_url",
    ):

        value = item.get(key)

        if isinstance(
            value,
            str,
        ) and value.strip():

            return value.strip()

    # Spotify-style images list.
    images = item.get(
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

            url = clean_text(
                image.get("url")
            )

            if url:
                return url

    # Album-level artwork.
    album = item.get(
        "album"
    )

    if isinstance(
        album,
        dict,
    ):

        album_cover = extract_cover(
            album
        )

        if album_cover:
            return album_cover

    return None


# ============================================================
# DURATION
# ============================================================

def format_duration(
    value: Any,
) -> str:
    """
    Format duration.

    Supports:
        milliseconds
        seconds
        already formatted strings
    """

    if value is None:
        return ""

    if isinstance(
        value,
        str,
    ):

        value = value.strip()

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

    # Spotify duration_ms is normally > 10000.
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
# RESPONSE EXTRACTION
# ============================================================

def find_track_items(
    payload: Any,
) -> list[dict[str, Any]]:
    """
    Extract track objects from the API response.

    Supports multiple common response structures.
    """

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

        # tracks: [...]
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

        # tracks: {items: [...]}
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

        # items: [...]
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

    return []


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_track(
    item: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert an API result into the bot's
    internal Spotify track format.
    """

    spotify_id = clean_text(
        item.get("id")
        or item.get("track_id")
        or item.get("spotify_id")
    )

    title = clean_text(
        item.get("title")
        or item.get("name"),
        "Unknown Track",
    )

    artist = extract_artist(
        item
    )

    album_data = item.get(
        "album"
    )

    if isinstance(
        album_data,
        dict,
    ):

        album = clean_text(
            album_data.get("name")
            or album_data.get("title"),
            "Unknown Album",
        )

    else:

        album = clean_text(
            album_data,
            "Unknown Album",
        )

    duration = (
        item.get("duration")
        or item.get("duration_ms")
        or item.get("length")
    )

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

    if not spotify_url and spotify_id:
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
# RAPIDAPI SEARCH
# ============================================================

def search_tracks(
    query: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    Search Spotify tracks using RapidAPI.

    Parameters:

        q
        type=tracks
        limit
    """

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
        "Spotify search: %s",
        query,
    )

    try:

        response = requests.get(
            SPOTIFY_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=SPOTIFY_TIMEOUT,
        )

    except requests.RequestException as exc:

        logger.exception(
            "Spotify API request failed."
        )

        raise RuntimeError(
            "Unable to connect to the Spotify API."
        ) from exc

    # --------------------------------------------------------
    # RATE LIMIT
    # --------------------------------------------------------

    if response.status_code == 429:

        reset = response.headers.get(
            "X-RateLimit-Reset"
        )

        if reset:
            raise RuntimeError(
                "Spotify API rate limit reached. "
                f"Try again after {reset}."
            )

        raise RuntimeError(
            "Spotify API rate limit reached. "
            "Please try again later."
        )

    # --------------------------------------------------------
    # AUTH
    # --------------------------------------------------------

    if response.status_code in (
        401,
        403,
    ):

        raise RuntimeError(
            "RapidAPI authentication failed. "
            "Check RAPIDAPI_KEY and your API subscription."
        )

    # --------------------------------------------------------
    # NOT FOUND
    # --------------------------------------------------------

    if response.status_code == 404:

        raise RuntimeError(
            "Spotify Search endpoint was not found. "
            "Check RAPIDAPI_HOST."
        )

    # --------------------------------------------------------
    # OTHER ERRORS
    # --------------------------------------------------------

    if not response.ok:

        body = response.text[:500]

        raise RuntimeError(
            "Spotify API error "
            f"{response.status_code}: {body}"
        )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    try:

        payload = response.json()

    except ValueError as exc:

        raise RuntimeError(
            "Spotify API returned invalid JSON."
        ) from exc

    # --------------------------------------------------------
    # API SUCCESS FLAG
    # --------------------------------------------------------

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

            raise RuntimeError(
                clean_text(
                    error,
                    "Spotify API request failed.",
                )
            )

    # --------------------------------------------------------
    # RESULTS
    # --------------------------------------------------------

    items = find_track_items(
        payload
    )

    return [
        normalize_track(item)
        for item in items[:limit]
    ]


# ============================================================
# SEARCH FOR USER QUERY
# ============================================================

def search_spotify(
    user_text: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    Process a user Spotify request.

    Example:

        "Apna Bana Le - spotify"

    Returns:

        {
            "query": "Apna Bana Le",
            "results": [...],
            "is_spotify": True
        }
    """

    if not is_spotify_search(
        user_text
    ):

        return {
            "is_spotify": False,
            "query": clean_text(
                user_text
            ),
            "results": [],
        }

    query = clean_spotify_query(
        user_text
    )

    # A direct Spotify URL currently gives
    # us the Spotify ID, but the Search endpoint
    # requires q text. We therefore don't pretend
    # the URL itself is a search query.
    if is_spotify_url(query):

        return {
            "is_spotify": True,
            "is_url": True,
            "query": "",
            "spotify_url": query,
            "track_id": extract_spotify_track_id(
                query
            ),
            "results": [],
        }

    if not query:

        return {
            "is_spotify": True,
            "is_url": False,
            "query": "",
            "results": [],
        }

    results = search_tracks(
        query,
        limit=limit,
    )

    return {
        "is_spotify": True,
        "is_url": False,
        "query": query,
        "results": results,
    }


# ============================================================
# TELEGRAM RESULT FORMAT
# ============================================================

def format_result(
    track: dict[str, Any],
    number: int,
) -> str:
    """
    Format one search result for Telegram.
    """

    title = escape_html(
        track.get("title"),
    )

    artist = escape_html(
        track.get("artist"),
    )

    album = escape_html(
        track.get("album"),
    )

    duration = escape_html(
        track.get("duration"),
    )

    lines = [
        f"<b>{number}. {title}</b>",
        f"👤 {artist}",
    ]

    if album:
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
    """
    Format the complete Spotify search
    result list for Telegram.
    """

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

        if index != len(results):
            lines.append("")

    return "\n".join(
        lines
    )


# ============================================================
# STATUS
# ============================================================

def spotify_status() -> dict[str, Any]:
    """
    Return safe integration status.
    """

    return {
        "configured": bool(
            RAPIDAPI_KEY
        ),
        "host": RAPIDAPI_HOST,
        "search_url": SPOTIFY_SEARCH_URL,
        "search_type": "tracks",
        "search_limit": SPOTIFY_SEARCH_LIMIT,
    }


# ============================================================
# USER-FRIENDLY ERRORS
# ============================================================

def friendly_error(
    error: Exception,
) -> str:
    """
    Convert exceptions into Telegram-friendly messages.
    """

    message = clean_text(
        error
    )

    lowered = message.lower()

    if "rate limit" in lowered:

        return (
            "⏳ <b>Spotify search is temporarily "
            "rate limited.</b>\n\n"
            "Please try again shortly."
        )

    if (
        "authentication" in lowered
        or "rapidapi_key" in lowered
    ):

        return (
            "❌ <b>Spotify API authentication failed.</b>\n\n"
            "Check the RapidAPI key and subscription."
        )

    if "not found" in lowered:

        return (
            "❌ <b>Spotify Search API was not found.</b>\n\n"
            "Check the configured RapidAPI host."
        )

    return (
        "❌ <b>Spotify search failed.</b>\n\n"
        f"<code>{escape_html(message[:400])}</code>"
    )
