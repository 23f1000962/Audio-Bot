"""
Spotify metadata integration for Audio Bot.

Supported:
    - Spotify track URLs
    - Spotify album URLs
    - Spotify playlist URLs

This module retrieves Spotify metadata only.
It does NOT download, rip, or extract Spotify audio streams.

Required environment variables:
    SPOTIFY_CLIENT_ID
    SPOTIFY_CLIENT_SECRET
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials


logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()


# Spotify URL formats:
#
# https://open.spotify.com/track/xxxxxxxxxxxxxxxxxxxx
# https://open.spotify.com/album/xxxxxxxxxxxxxxxxxxxx
# https://open.spotify.com/playlist/xxxxxxxxxxxxxxxxxxxx
#
# Also accepts URLs containing query parameters such as:
# ?si=xxxxxxxx
# ?nd=1
#
SPOTIFY_URL_PATTERN = re.compile(
    r"https?://(?:open\.)?spotify\.com/"
    r"(track|album|playlist)/"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)


# Spotify IDs are normally 22 characters.
SPOTIFY_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{22}$")


# ============================================================
# CLIENT
# ============================================================

_spotify_client: spotipy.Spotify | None = None


def is_spotify_configured() -> bool:
    """
    Return True if Spotify credentials are configured.
    """
    return bool(
        SPOTIFY_CLIENT_ID
        and SPOTIFY_CLIENT_SECRET
    )


def get_spotify_client() -> spotipy.Spotify:
    """
    Create and cache a Spotify API client.

    Uses Spotify Client Credentials authentication.
    """
    global _spotify_client

    if _spotify_client is not None:
        return _spotify_client

    if not is_spotify_configured():
        raise RuntimeError(
            "Spotify is not configured. "
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
        )

    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
    )

    _spotify_client = spotipy.Spotify(
        auth_manager=auth_manager,
        requests_timeout=15,
        retries=2,
    )

    logger.info("Spotify client initialized")

    return _spotify_client


# ============================================================
# URL HELPERS
# ============================================================

def is_spotify_url(url: str) -> bool:
    """
    Check whether a string is a supported Spotify URL.
    """
    if not url:
        return False

    return bool(SPOTIFY_URL_PATTERN.search(url.strip()))


def parse_spotify_url(url: str) -> tuple[str, str] | None:
    """
    Extract Spotify resource type and Spotify ID.

    Returns:
        ("track", "spotify_id")
        ("album", "spotify_id")
        ("playlist", "spotify_id")

    Returns None if the URL is unsupported.
    """
    if not url:
        return None

    match = SPOTIFY_URL_PATTERN.search(url.strip())

    if not match:
        return None

    resource_type = match.group(1).lower()
    spotify_id = match.group(2)

    return resource_type, spotify_id


def is_valid_spotify_id(spotify_id: str) -> bool:
    """
    Validate a Spotify ID.
    """
    if not spotify_id:
        return False

    return bool(
        SPOTIFY_ID_PATTERN.fullmatch(
            spotify_id.strip()
        )
    )


# ============================================================
# GENERAL HELPERS
# ============================================================

def _safe_text(value: Any, default: str = "") -> str:
    """
    Convert a value to clean text safely.
    """
    if value is None:
        return default

    return str(value).strip()


def _get_artists(item: dict[str, Any]) -> list[str]:
    """
    Extract artist names from a Spotify track-like object.
    """
    artists = item.get("artists") or []

    result = []

    for artist in artists:
        if not isinstance(artist, dict):
            continue

        name = _safe_text(artist.get("name"))

        if name:
            result.append(name)

    return result


def _get_artist_string(item: dict[str, Any]) -> str:
    """
    Return artists as a comma-separated string.
    """
    artists = _get_artists(item)

    return ", ".join(artists) if artists else "Unknown Artist"


def _get_image_url(item: dict[str, Any]) -> str | None:
    """
    Get the first available Spotify image URL.
    """
    images = item.get("images") or []

    if not images:
        return None

    for image in images:
        if not isinstance(image, dict):
            continue

        url = _safe_text(image.get("url"))

        if url:
            return url

    return None


def _format_duration(milliseconds: Any) -> str:
    """
    Convert Spotify duration in milliseconds to MM:SS or HH:MM:SS.
    """
    try:
        milliseconds = int(milliseconds)
    except (TypeError, ValueError):
        return "Unknown"

    if milliseconds <= 0:
        return "Unknown"

    total_seconds = milliseconds // 1000

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def _spotify_url(
    resource_type: str,
    spotify_id: str,
) -> str:
    """
    Build a canonical Spotify URL.
    """
    return (
        f"https://open.spotify.com/"
        f"{resource_type}/{spotify_id}"
    )


# ============================================================
# TRACK
# ============================================================

def get_track(
    spotify_id: str,
    market: str = "IN",
) -> dict[str, Any]:
    """
    Fetch metadata for one Spotify track.

    Returns a normalized dictionary.
    """
    if not is_valid_spotify_id(spotify_id):
        raise ValueError("Invalid Spotify track ID.")

    spotify = get_spotify_client()

    track = spotify.track(
        spotify_id,
        market=market,
    )

    if not track:
        raise RuntimeError(
            "Spotify returned no track information."
        )

    track_id = _safe_text(track.get("id"), spotify_id)

    album = track.get("album") or {}

    return {
        "type": "track",
        "id": track_id,
        "title": _safe_text(
            track.get("name"),
            "Unknown Track",
        ),
        "artist": _get_artist_string(track),
        "artists": _get_artists(track),
        "album": _safe_text(
            album.get("name"),
            "Unknown Album",
        ),
        "duration_ms": track.get("duration_ms"),
        "duration": _format_duration(
            track.get("duration_ms")
        ),
        "explicit": bool(
            track.get("explicit", False)
        ),
        "preview_url": track.get("preview_url"),
        "image_url": _get_image_url(album),
        "spotify_url": (
            (track.get("external_urls") or {}).get(
                "spotify"
            )
            or _spotify_url("track", track_id)
        ),
    }


# ============================================================
# ALBUM
# ============================================================

def get_album(
    spotify_id: str,
    market: str = "IN",
) -> dict[str, Any]:
    """
    Fetch album metadata and its tracks.
    """
    if not is_valid_spotify_id(spotify_id):
        raise ValueError("Invalid Spotify album ID.")

    spotify = get_spotify_client()

    album = spotify.album(
        spotify_id,
        market=market,
    )

    if not album:
        raise RuntimeError(
            "Spotify returned no album information."
        )

    album_id = _safe_text(
        album.get("id"),
        spotify_id,
    )

    tracks = []

    track_page = album.get("tracks") or {}

    for item in track_page.get("items") or []:
        if not isinstance(item, dict):
            continue

        track_id = _safe_text(item.get("id"))

        if not track_id:
            continue

        tracks.append(
            {
                "type": "track",
                "id": track_id,
                "title": _safe_text(
                    item.get("name"),
                    "Unknown Track",
                ),
                "artist": _get_artist_string(item),
                "artists": _get_artists(item),
                "duration_ms": item.get("duration_ms"),
                "duration": _format_duration(
                    item.get("duration_ms")
                ),
                "track_number": item.get(
                    "track_number"
                ),
                "explicit": bool(
                    item.get("explicit", False)
                ),
                "spotify_url": (
                    (item.get("external_urls") or {}).get(
                        "spotify"
                    )
                    or _spotify_url(
                        "track",
                        track_id,
                    )
                ),
            }
        )

    return {
        "type": "album",
        "id": album_id,
        "title": _safe_text(
            album.get("name"),
            "Unknown Album",
        ),
        "artist": _get_artist_string(album),
        "artists": _get_artists(album),
        "release_date": _safe_text(
            album.get("release_date")
        ),
        "total_tracks": album.get(
            "total_tracks",
            len(tracks),
        ),
        "image_url": _get_image_url(album),
        "tracks": tracks,
        "spotify_url": (
            (album.get("external_urls") or {}).get(
                "spotify"
            )
            or _spotify_url(
                "album",
                album_id,
            )
        ),
    }


# ============================================================
# PLAYLIST
# ============================================================

def get_playlist(
    spotify_id: str,
    market: str = "IN",
    max_tracks: int = 100,
) -> dict[str, Any]:
    """
    Fetch public Spotify playlist metadata.

    max_tracks prevents a huge playlist from generating
    an unnecessarily large API response.

    The playlist is returned as metadata only.
    """
    if not is_valid_spotify_id(spotify_id):
        raise ValueError(
            "Invalid Spotify playlist ID."
        )

    if max_tracks < 1:
        max_tracks = 1

    max_tracks = min(max_tracks, 500)

    spotify = get_spotify_client()

    playlist = spotify.playlist(
        spotify_id,
        market=market,
    )

    if not playlist:
        raise RuntimeError(
            "Spotify returned no playlist information."
        )

    playlist_id = _safe_text(
        playlist.get("id"),
        spotify_id,
    )

    tracks = []

    offset = 0
    page_size = 100

    while len(tracks) < max_tracks:
        remaining = max_tracks - len(tracks)

        limit = min(
            page_size,
            remaining,
        )

        page = spotify.playlist_items(
            playlist_id,
            market=market,
            limit=limit,
            offset=offset,
            additional_types=("track",),
        )

        items = page.get("items") or []

        if not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue

            track = item.get("track")

            if not isinstance(track, dict):
                continue

            track_id = _safe_text(
                track.get("id")
            )

            if not track_id:
                continue

            tracks.append(
                {
                    "type": "track",
                    "id": track_id,
                    "title": _safe_text(
                        track.get("name"),
                        "Unknown Track",
                    ),
                    "artist": _get_artist_string(
                        track
                    ),
                    "artists": _get_artists(
                        track
                    ),
                    "album": _safe_text(
                        (
                            track.get("album")
                            or {}
                        ).get("name"),
                        "Unknown Album",
                    ),
                    "duration_ms": track.get(
                        "duration_ms"
                    ),
                    "duration": _format_duration(
                        track.get(
                            "duration_ms"
                        )
                    ),
                    "explicit": bool(
                        track.get(
                            "explicit",
                            False,
                        )
                    ),
                    "spotify_url": (
                        (
                            track.get(
                                "external_urls"
                            )
                            or {}
                        ).get("spotify")
                        or _spotify_url(
                            "track",
                            track_id,
                        )
                    ),
                }
            )

            if len(tracks) >= max_tracks:
                break

        next_page = page.get("next")

        if not next_page:
            break

        offset += len(items)

        if len(items) == 0:
            break

    return {
        "type": "playlist",
        "id": playlist_id,
        "title": _safe_text(
            playlist.get("name"),
            "Unknown Playlist",
        ),
        "description": _safe_text(
            playlist.get("description")
        ),
        "owner": _safe_text(
            (
                playlist.get("owner")
                or {}
            ).get("display_name"),
            "Unknown",
        ),
        "total_tracks": (
            (
                playlist.get("tracks")
                or {}
            ).get(
                "total",
                len(tracks),
            )
        ),
        "tracks_loaded": len(tracks),
        "image_url": _get_image_url(
            playlist
        ),
        "tracks": tracks,
        "spotify_url": (
            (
                playlist.get(
                    "external_urls"
                )
                or {}
            ).get("spotify")
            or _spotify_url(
                "playlist",
                playlist_id,
            )
        ),
    }


# ============================================================
# UNIVERSAL SPOTIFY LOOKUP
# ============================================================

def get_spotify_info(
    url: str,
    market: str = "IN",
    max_playlist_tracks: int = 100,
) -> dict[str, Any]:
    """
    Detect a Spotify URL and retrieve its metadata.

    Supported:
        track
        album
        playlist

    Example:
        info = get_spotify_info(
            "https://open.spotify.com/track/..."
        )
    """
    parsed = parse_spotify_url(url)

    if not parsed:
        raise ValueError(
            "Unsupported Spotify URL."
        )

    resource_type, spotify_id = parsed

    if resource_type == "track":
        return get_track(
            spotify_id,
            market=market,
        )

    if resource_type == "album":
        return get_album(
            spotify_id,
            market=market,
        )

    if resource_type == "playlist":
        return get_playlist(
            spotify_id,
            market=market,
            max_tracks=max_playlist_tracks,
        )

    raise ValueError(
        f"Unsupported Spotify resource type: "
        f"{resource_type}"
    )


# ============================================================
# DISPLAY HELPERS
# ============================================================

def format_track_message(
    track: dict[str, Any],
) -> str:
    """
    Format a Spotify track for Telegram.
    """
    title = _safe_text(
        track.get("title"),
        "Unknown Track",
    )

    artist = _safe_text(
        track.get("artist"),
        "Unknown Artist",
    )

    album = _safe_text(
        track.get("album"),
        "Unknown Album",
    )

    duration = _safe_text(
        track.get("duration"),
        "Unknown",
    )

    spotify_url = _safe_text(
        track.get("spotify_url")
    )

    lines = [
        f"🎵 <b>{title}</b>",
        f"👤 {artist}",
        f"💿 {album}",
        f"⏱ {duration}",
    ]

    if spotify_url:
        lines.append(
            f'🔗 <a href="{spotify_url}">'
            f"Open in Spotify</a>"
        )

    return "\n".join(lines)


def format_album_message(
    album: dict[str, Any],
) -> str:
    """
    Format Spotify album metadata for Telegram.
    """
    title = _safe_text(
        album.get("title"),
        "Unknown Album",
    )

    artist = _safe_text(
        album.get("artist"),
        "Unknown Artist",
    )

    release_date = _safe_text(
        album.get("release_date"),
        "Unknown",
    )

    total_tracks = album.get(
        "total_tracks",
        0,
    )

    spotify_url = _safe_text(
        album.get("spotify_url")
    )

    lines = [
        f"💿 <b>{title}</b>",
        f"👤 {artist}",
        f"📅 {release_date}",
        f"🎵 {total_tracks} tracks",
    ]

    if spotify_url:
        lines.append(
            f'🔗 <a href="{spotify_url}">'
            f"Open in Spotify</a>"
        )

    return "\n".join(lines)


def format_playlist_message(
    playlist: dict[str, Any],
) -> str:
    """
    Format Spotify playlist metadata for Telegram.
    """
    title = _safe_text(
        playlist.get("title"),
        "Unknown Playlist",
    )

    owner = _safe_text(
        playlist.get("owner"),
        "Unknown",
    )

    total_tracks = playlist.get(
        "total_tracks",
        0,
    )

    tracks_loaded = playlist.get(
        "tracks_loaded",
        0,
    )

    spotify_url = _safe_text(
        playlist.get("spotify_url")
    )

    lines = [
        f"📋 <b>{title}</b>",
        f"👤 {owner}",
        f"🎵 {total_tracks} tracks",
        f"📥 Metadata loaded: {tracks_loaded}",
    ]

    if spotify_url:
        lines.append(
            f'🔗 <a href="{spotify_url}">'
            f"Open in Spotify</a>"
        )

    return "\n".join(lines)


# ============================================================
# ERROR HANDLING
# ============================================================

def get_spotify_error_message(
    error: Exception,
) -> str:
    """
    Convert common Spotify errors into user-friendly
    Telegram messages.
    """
    message = str(error).strip()

    if not message:
        message = "Unknown Spotify error."

    lowered = message.lower()

    if (
        "401" in lowered
        or "invalid client" in lowered
        or "token" in lowered
    ):
        return (
            "❌ Spotify authentication failed.\n\n"
            "Check SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET."
        )

    if "403" in lowered:
        return (
            "❌ Spotify denied access to this resource.\n\n"
            "The link may require user authorization "
            "or may not be publicly accessible."
        )

    if "404" in lowered:
        return (
            "❌ Spotify item not found.\n\n"
            "The track, album, or playlist may have "
            "been removed or is unavailable."
        )

    if "429" in lowered:
        return (
            "⏳ Spotify rate limit reached.\n\n"
            "Please try again shortly."
        )

    return (
        "❌ Unable to read this Spotify link.\n\n"
        f"<code>{message[:300]}</code>"
    )


# ============================================================
# MODULE STATUS
# ============================================================

def spotify_status() -> dict[str, Any]:
    """
    Return Spotify integration status.
    """
    return {
        "configured": is_spotify_configured(),
        "client_id_present": bool(
            SPOTIFY_CLIENT_ID
        ),
        "client_secret_present": bool(
            SPOTIFY_CLIENT_SECRET
        ),
        "supported_types": [
            "track",
            "album",
            "playlist",
        ],
        "audio_download": False,
    }
