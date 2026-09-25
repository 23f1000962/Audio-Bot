"""
Spotify metadata/search integration for Audio Bot.

Spotify is used only as a metadata/search source.

Audio flow:
    Spotify metadata -> YouTube search -> yt-dlp -> FFmpeg -> Telegram

Supported:
    Apna Bana Le spotify
    spotify Apna Bana Le
    Apna Bana Le - spotify
    https://open.spotify.com/track/<id>

Important:
    This module never calls Spotify/RapidAPI audio-download endpoints.
"""

from __future__ import annotations

import html
import logging
import os
import re
from difflib import SequenceMatcher
from typing import Any

import requests


logger = logging.getLogger(__name__)

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.getenv(
    "RAPIDAPI_HOST",
    "spotify-downloader9.p.rapidapi.com",
).strip()

RAPIDAPI_SEARCH_URL = f"https://{RAPIDAPI_HOST}/search"
SPOTIFY_OEMBED_URL = "https://open.spotify.com/oembed"

SPOTIFY_TIMEOUT = max(5, int(os.getenv("SPOTIFY_TIMEOUT", "20")))
SPOTIFY_SEARCH_LIMIT = max(
    1,
    min(10, int(os.getenv("SPOTIFY_SEARCH_LIMIT", "8"))),
)

SPOTIFY_URL_PATTERN = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?"
    r"(track|album|playlist|artist)/"
    r"([A-Za-z0-9]+)"
    r"(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)


def clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize_key(value: Any) -> str:
    text = clean_text(value).lower()
    text = html.unescape(text)
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(value: Any) -> set[str]:
    return {
        token
        for token in normalize_key(value).split()
        if len(token) > 1
    }


def is_spotify_url(text: str) -> bool:
    return bool(text and SPOTIFY_URL_PATTERN.search(text.strip()))


def parse_spotify_url(url: str) -> tuple[str, str] | None:
    if not url:
        return None

    match = SPOTIFY_URL_PATTERN.search(url.strip())
    if not match:
        return None

    return match.group(1).lower(), match.group(2)


def build_spotify_url(spotify_id: str) -> str:
    return f"https://open.spotify.com/track/{spotify_id}"


def build_spotify_resource_url(resource_type: str, resource_id: str) -> str:
    return f"https://open.spotify.com/{resource_type}/{resource_id}"


def is_spotify_search(text: str) -> bool:
    if not text:
        return False

    value = text.strip()

    if is_spotify_url(value):
        return True

    return bool(
        re.search(r"(?:^|\s)spotify\s*$", value, re.I)
        or re.search(r"\s*-\s*spotify\s*$", value, re.I)
        or re.match(r"^\s*spotify\s+.+", value, re.I)
        or re.match(r"^\s*spotify\s*-\s*.+", value, re.I)
    )


def clean_spotify_query(text: str) -> str:
    if not text:
        return ""

    query = text.strip()

    if is_spotify_url(query):
        return query

    query = re.sub(
        r"^\s*spotify\s*-\s*",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"^\s*spotify\s+",
        "",
        query,
        count=1,
        flags=re.I,
    )
    query = re.sub(
        r"\s*-\s*spotify\s*$",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"\s+spotify\s*$",
        "",
        query,
        flags=re.I,
    )

    return query.strip()


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _name_from_artist(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()

    if not isinstance(value, dict):
        return ""

    direct = (
        value.get("name")
        or value.get("artistName")
        or value.get("artist_name")
    )
    if direct:
        return clean_text(direct)

    profile = value.get("profile")
    if isinstance(profile, dict):
        name = clean_text(profile.get("name"))
        if name:
            return name

    return ""


def extract_artists(item: Any) -> list[str]:
    """
    Handles normal Spotify objects and nested RapidAPI shapes, including:
        data.artists.items[].profile.name
        itemV2.data.artists.items[].profile.name
        artists[].name
    """
    result: list[str] = []

    def add(value: Any):
        if isinstance(value, list):
            for child in value:
                add(child)
            return

        if isinstance(value, dict):
            name = _name_from_artist(value)
            if name:
                result.append(name)

            for key in ("items", "artists", "artist"):
                child = value.get(key)
                if child is not None:
                    add(child)
            return

        if isinstance(value, str):
            for part in value.split(","):
                part = part.strip()
                if part:
                    result.append(part)

    if isinstance(item, dict):
        for key in (
            "artists",
            "artist",
            "artistName",
            "artist_name",
            "performers",
            "data",
            "item",
            "itemV2",
        ):
            if key in item:
                add(item.get(key))

    unique = []
    seen = set()

    for artist in result:
        key = normalize_key(artist)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(artist)

    return unique


def extract_title(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    for key in ("title", "name", "trackName", "track_name"):
        value = clean_text(item.get(key))
        if value:
            return value

    for key in ("data", "item", "itemV2", "track", "trackV2"):
        title = extract_title(item.get(key))
        if title:
            return title

    return ""


def extract_album(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    for key in ("albumName", "album_name"):
        value = clean_text(item.get(key))
        if value:
            return value

    album = item.get("album")

    if isinstance(album, str):
        return album.strip()

    if isinstance(album, dict):
        value = clean_text(album.get("name") or album.get("title"))
        if value:
            return value

    for key in ("albumOfTrack", "albumOfTrackV2"):
        child = item.get(key)
        if isinstance(child, dict):
            value = clean_text(child.get("name") or child.get("title"))
            if value:
                return value

    return ""


def extract_track_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    for key in ("id", "track_id", "spotify_id"):
        value = clean_text(item.get(key))
        if value:
            return value

    uri = clean_text(item.get("uri"))

    if uri.startswith("spotify:track:"):
        return uri.split("spotify:track:", 1)[1].strip()

    for key in ("data", "item", "itemV2", "track", "trackV2"):
        value = extract_track_id(item.get(key))
        if value:
            return value

    return ""


def extract_cover(item: Any) -> str | None:
    if not isinstance(item, dict):
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
        value = clean_text(item.get(key))
        if value:
            return value

    images = item.get("images")

    if isinstance(images, list):
        for image in images:
            if isinstance(image, str) and image.strip():
                return image.strip()

            if isinstance(image, dict):
                value = clean_text(image.get("url"))
                if value:
                    return value

    cover_art = item.get("coverArt")

    if isinstance(cover_art, dict):
        sources = cover_art.get("sources")

        if isinstance(sources, list):
            for source in reversed(sources):
                if isinstance(source, dict):
                    value = clean_text(source.get("url"))
                    if value:
                        return value

    for key in ("data", "item", "itemV2", "album", "albumOfTrack"):
        value = extract_cover(item.get(key))
        if value:
            return value

    return None


def format_duration(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("totalMilliseconds")
            or value.get("milliseconds")
            or value.get("duration_ms")
            or value.get("durationMs")
        )

    if value is None:
        return ""

    if isinstance(value, str):
        value = value.strip()

        if not value:
            return ""

        if ":" in value:
            return value

    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""

    total_seconds = int(number / 1000) if number > 10000 else int(number)

    if total_seconds <= 0:
        return ""

    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def extract_duration(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    for key in (
        "duration",
        "duration_ms",
        "durationMs",
        "length",
    ):
        value = format_duration(item.get(key))
        if value:
            return value

    for key in (
        "trackDuration",
        "duration",
        "data",
        "item",
        "itemV2",
        "track",
        "trackV2",
    ):
        child = item.get(key)

        if isinstance(child, dict):
            value = format_duration(child)

            if value:
                return value

            value = extract_duration(child)

            if value:
                return value

    return ""


def _find_spotify_url(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    external_urls = item.get("external_urls")

    if isinstance(external_urls, dict):
        value = clean_text(external_urls.get("spotify"))

        if value:
            return value

    for key in ("data", "item", "itemV2", "track", "trackV2"):
        value = _find_spotify_url(item.get(key))

        if value:
            return value

    return ""


def normalize_track(item: dict[str, Any]) -> dict[str, Any]:
    title = extract_title(item)
    artists = extract_artists(item)
    artist = ", ".join(artists) if artists else "Unknown Artist"

    spotify_id = extract_track_id(item)
    spotify_url = _find_spotify_url(item)

    if not spotify_url and spotify_id:
        spotify_url = build_spotify_url(spotify_id)

    return {
        "id": spotify_id,
        "title": title or "Unknown Track",
        "artist": artist,
        "artists": artists,
        "album": extract_album(item) or "Unknown Album",
        "cover": extract_cover(item),
        "duration": extract_duration(item),
        "spotify_url": spotify_url,
        "raw": item,
    }


def _looks_like_track(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False

    item_type = normalize_key(
        item.get("type")
        or item.get("entityType")
        or item.get("contentType")
    )

    if item_type in {"track", "song"}:
        return True

    title = extract_title(item)

    if not title:
        return False

    return bool(
        extract_track_id(item)
        or extract_duration(item)
        or extract_artists(item)
    )


def _track_candidates(payload: Any) -> list[dict[str, Any]]:
    """
    Prefer explicit track collections before recursive discovery.
    This prevents generic items arrays from mixing unrelated entities.
    """
    candidates: list[dict[str, Any]] = []

    def add_list(value: Any):
        if isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    candidates.append(child)

    if isinstance(payload, dict):
        priority_paths = [
            ("tracks", "items"),
            ("data", "tracks", "items"),
            ("result", "tracks", "items"),
            ("results", "tracks", "items"),
            ("data", "items"),
            ("result", "items"),
            ("results", "items"),
            ("items",),
            ("songs",),
        ]

        for path in priority_paths:
            current: Any = payload

            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break

                current = current.get(key)

            add_list(current)

    if candidates:
        track_candidates = [
            item
            for item in candidates
            if _looks_like_track(item)
        ]

        if track_candidates:
            return track_candidates

    return [
        item
        for item in _walk_dicts(payload)
        if _looks_like_track(item)
    ]


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(
        None,
        normalize_key(a),
        normalize_key(b),
    ).ratio()


def _relevance_score(
    track: dict[str, Any],
    query: str,
) -> float:
    title = track.get("title", "")
    artist = track.get("artist", "")

    q_tokens = tokenize(query)
    title_tokens = tokenize(title)
    artist_tokens = tokenize(artist)

    if not q_tokens:
        return 0.0

    overlap = len(q_tokens & title_tokens) / len(q_tokens)
    artist_overlap = len(q_tokens & artist_tokens) / len(q_tokens)

    score = overlap * 65.0
    score += _similarity(query, title) * 25.0
    score += artist_overlap * 10.0

    if normalize_key(query) == normalize_key(title):
        score += 25.0

    if title_tokens and not (q_tokens & title_tokens):
        score -= 45.0

    return score


def rank_tracks(
    tracks: list[dict[str, Any]],
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    scored = []

    for track in tracks:
        if not track or not track.get("title"):
            continue

        copy = dict(track)
        copy["_score"] = round(
            _relevance_score(track, query),
            2,
        )
        scored.append(copy)

    scored.sort(
        key=lambda item: item.get("_score", 0),
        reverse=True,
    )

    filtered = [
        item
        for item in scored
        if item.get("_score", 0) >= 18
    ]

    return (filtered or scored)[:limit]


def deduplicate_tracks(
    tracks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique = []
    seen_ids = set()
    seen_fingerprints = set()

    for track in tracks:
        if not track:
            continue

        title = clean_text(track.get("title"))
        artist = clean_text(track.get("artist"))

        if not title or title == "Unknown Track":
            continue

        spotify_id = clean_text(track.get("id"))

        if spotify_id:
            key = normalize_key(spotify_id)

            if key in seen_ids:
                continue

            seen_ids.add(key)

        else:
            fingerprint = "|".join(
                [
                    normalize_key(title),
                    normalize_key(artist),
                    normalize_key(track.get("duration")),
                ]
            )

            if fingerprint in seen_fingerprints:
                continue

            seen_fingerprints.add(fingerprint)

        unique.append(track)

    return unique


def _request_rapidapi_search(
    query: str,
    limit: int,
) -> Any:
    if not RAPIDAPI_KEY:
        raise RuntimeError(
            "RAPIDAPI_KEY is not configured."
        )

    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }

    params = {
        "q": query,
        "type": "tracks",
        "limit": limit,
    }

    response = requests.get(
        RAPIDAPI_SEARCH_URL,
        headers=headers,
        params=params,
        timeout=SPOTIFY_TIMEOUT,
    )

    response.raise_for_status()

    return response.json()


def fetch_spotify_oembed(
    url: str,
) -> dict[str, Any]:
    response = requests.get(
        SPOTIFY_OEMBED_URL,
        params={"url": url},
        timeout=SPOTIFY_TIMEOUT,
        headers={
            "User-Agent": "Audio-Bot/2.3",
        },
    )

    response.raise_for_status()

    payload = response.json()

    return (
        payload
        if isinstance(payload, dict)
        else {}
    )


def search_tracks(
    query: str,
    limit: int = SPOTIFY_SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    query = clean_text(query)

    if not query:
        return []

    payload = _request_rapidapi_search(
        query,
        min(max(limit * 2, 8), 20),
    )

    raw_candidates = _track_candidates(
        payload
    )

    normalized = [
        normalize_track(item)
        for item in raw_candidates
    ]

    normalized = deduplicate_tracks(
        normalized
    )

    ranked = rank_tracks(
        normalized,
        query,
        limit,
    )

    for track in ranked:
        track.pop("_score", None)

    return ranked


def search_spotify(
    text: str,
    limit: int = SPOTIFY_SEARCH_LIMIT,
) -> dict[str, Any]:
    """
    Resolve a Spotify search query or URL.

    Direct track URL:
        Spotify oEmbed -> track title
        RapidAPI search -> metadata
        relevance ranking -> result
        existing bot flow -> YouTube -> yt-dlp

    Album/playlist/artist URLs are recognized through oEmbed, but this
    module does not call any Spotify/RapidAPI audio-download endpoint.
    """
    original = clean_text(text)
    parsed = parse_spotify_url(original)

    if parsed:
        resource_type, resource_id = parsed

        resource_url = build_spotify_resource_url(
            resource_type,
            resource_id,
        )

        if resource_type == "track":
            oembed = fetch_spotify_oembed(
                resource_url
            )

            query = clean_text(
                oembed.get("title")
            )

            if not query:
                raise RuntimeError(
                    "Spotify oEmbed did not return a track title."
                )

            results = search_tracks(
                query,
                limit=max(1, min(limit, 5)),
            )

            for index, track in enumerate(results):
                if track.get("id") == resource_id:
                    results.insert(
                        0,
                        results.pop(index),
                    )
                    break

            return {
                "mode": "url",
                "resource_type": resource_type,
                "resource_id": resource_id,
                "query": query,
                "results": results[:limit],
                "spotify_url": resource_url,
                "oembed": oembed,
            }

        oembed = fetch_spotify_oembed(
            resource_url
        )

        return {
            "mode": "url",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "query": clean_text(
                oembed.get("title")
            ),
            "results": [],
            "spotify_url": resource_url,
            "oembed": oembed,
        }

    query = clean_spotify_query(
        original
    )

    if not query:
        return {
            "mode": "search",
            "resource_type": None,
            "resource_id": None,
            "query": "",
            "results": [],
        }

    return {
        "mode": "search",
        "resource_type": None,
        "resource_id": None,
        "query": query,
        "results": search_tracks(
            query,
            limit,
        ),
    }


def spotify_status() -> dict[str, Any]:
    return {
        "enabled": bool(RAPIDAPI_KEY),
        "host": RAPIDAPI_HOST,
        "search": True,
        "oembed": True,
        "audio_download": False,
    }
