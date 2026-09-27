import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv("/app/.env")
load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

# One RapidAPI key is used for both Spotify APIs.
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()

# Spotify API40
SPOTIFY_API40_HOST = os.getenv(
    "SPOTIFY_API40_HOST",
    "spotify-api40.p.rapidapi.com",
).strip()

# Spotify Downloader9
RAPIDAPI_HOST = os.getenv(
    "RAPIDAPI_HOST",
    "spotify-downloader9.p.rapidapi.com",
).strip()


API40_SEARCH_URL = f"https://{SPOTIFY_API40_HOST}/search"
API40_TRACK_URL = f"https://{SPOTIFY_API40_HOST}/track"

DOWNLOADER_URL = f"https://{RAPIDAPI_HOST}/downloadSong"


# ============================================================
# CONSTANTS
# ============================================================

REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120
MAX_SEARCH_RESULTS = 10

DOWNLOAD_ROOT = Path(
    os.getenv("DOWNLOAD_DIR", "/app/downloads")
)

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)


# ============================================================
# EXCEPTIONS
# ============================================================

class SpotifyError(Exception):
    """Base Spotify error."""


class SpotifyConfigurationError(SpotifyError):
    """Spotify environment variables are missing."""


class SpotifyAPIError(SpotifyError):
    """Spotify API returned an error."""


class SpotifyDownloadError(SpotifyError):
    """Spotify audio download failed."""


# ============================================================
# HELPERS
# ============================================================

def spotify_configured() -> bool:
    """
    Both Spotify APIs use the same RapidAPI key.
    """
    return bool(
        RAPIDAPI_KEY
        and SPOTIFY_API40_HOST
        and RAPIDAPI_HOST
    )


def _require_configuration() -> None:
    if not RAPIDAPI_KEY:
        raise SpotifyConfigurationError(
            "RAPIDAPI_KEY is not configured."
        )


def _api40_headers() -> Dict[str, str]:
    """
    Headers for Spotify API40.

    IMPORTANT:
    API40 uses the same RapidAPI key as Downloader9.
    """
    _require_configuration()

    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": SPOTIFY_API40_HOST,
        "Accept": "application/json",
    }


def _downloader_headers() -> Dict[str, str]:
    """
    Headers for Spotify Downloader9.

    Uses the same RAPIDAPI_KEY.
    """
    _require_configuration()

    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Accept": "application/json",
    }


def _safe_filename(value: str, fallback: str = "spotify_audio") -> str:
    value = str(value or "").strip()

    if not value:
        value = fallback

    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value[:180] or fallback


def _unwrap(value: Any) -> Any:
    """
    Spotify API responses sometimes wrap objects inside:
    item / itemV2 / track / data.
    """
    if not isinstance(value, dict):
        return value

    for key in ("item", "itemV2", "track", "data"):
        candidate = value.get(key)

        if isinstance(candidate, dict):
            return candidate

    return value


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _extract_artists(track: Dict[str, Any]) -> str:
    """
    Handles multiple Spotify response formats.
    """

    # Format:
    # artists.items[].profile.name
    artists = track.get("artists")

    if isinstance(artists, dict):
        items = artists.get("items")

        if isinstance(items, list):
            names = []

            for item in items:
                if not isinstance(item, dict):
                    continue

                profile = item.get("profile")

                if isinstance(profile, dict):
                    name = profile.get("name")

                    if name:
                        names.append(str(name))
                        continue

                name = item.get("name")

                if name:
                    names.append(str(name))

            if names:
                return ", ".join(names)

    # Simple:
    # artists: [...]
    if isinstance(artists, list):
        names = []

        for artist in artists:
            if isinstance(artist, dict):
                name = (
                    artist.get("name")
                    or artist.get("title")
                )

                if name:
                    names.append(str(name))

            elif isinstance(artist, str):
                names.append(artist)

        if names:
            return ", ".join(names)

    # Other Spotify API40 formats
    artist_of_track = track.get("artistOfTrack")

    if isinstance(artist_of_track, dict):
        name = artist_of_track.get("name")

        if name:
            return str(name)

    if isinstance(artist_of_track, list):
        names = []

        for artist in artist_of_track:
            if isinstance(artist, dict):
                name = artist.get("name")

                if name:
                    names.append(str(name))

        if names:
            return ", ".join(names)

    return ""


def _extract_album(track: Dict[str, Any]) -> str:
    album = track.get("album")

    if isinstance(album, dict):
        name = album.get("name")

        if name:
            return str(name)

    album_of_track = track.get("albumOfTrack")

    if isinstance(album_of_track, dict):
        name = album_of_track.get("name")

        if name:
            return str(name)

    return ""


def _extract_duration(track: Dict[str, Any]) -> Optional[int]:
    """
    Returns duration in seconds.
    """

    possible_values = [
        track.get("duration_ms"),
        track.get("durationMs"),
        track.get("duration"),
    ]

    duration_obj = track.get("duration")

    if isinstance(duration_obj, dict):
        possible_values.extend([
            duration_obj.get("totalMilliseconds"),
            duration_obj.get("milliseconds"),
        ])

    track_duration = track.get("trackDuration")

    if isinstance(track_duration, dict):
        possible_values.extend([
            track_duration.get("totalMilliseconds"),
            track_duration.get("milliseconds"),
        ])

    for value in possible_values:
        if value is None:
            continue

        try:
            value = float(value)

            # Milliseconds
            if value > 1000:
                return max(1, int(value / 1000))

            # Seconds
            return max(1, int(value))

        except (TypeError, ValueError):
            continue

    return None


def _extract_artwork(track: Dict[str, Any]) -> str:
    """
    Extract album cover URL.
    """

    # albumOfTrack.coverArt.sources
    album = track.get("albumOfTrack")

    if isinstance(album, dict):
        cover_art = album.get("coverArt")

        if isinstance(cover_art, dict):
            sources = cover_art.get("sources")

            if isinstance(sources, list):
                for source in sources:
                    if isinstance(source, dict):
                        url = source.get("url")

                        if url:
                            return str(url)

    # album.coverArt
    album = track.get("album")

    if isinstance(album, dict):
        cover_art = album.get("coverArt")

        if isinstance(cover_art, dict):
            sources = cover_art.get("sources")

            if isinstance(sources, list):
                for source in sources:
                    if isinstance(source, dict):
                        url = source.get("url")

                        if url:
                            return str(url)

    # Simple image fields
    for key in (
        "cover",
        "coverUrl",
        "image",
        "imageUrl",
        "thumbnail",
    ):
        value = track.get(key)

        if isinstance(value, str) and value.startswith("http"):
            return value

    return ""


# ============================================================
# TRACK PARSING
# ============================================================

def _normalise_track(raw: Any) -> Optional[Dict[str, Any]]:
    """
    Convert different Spotify API40 track structures into one
    consistent structure.
    """

    if not isinstance(raw, dict):
        return None

    track = _unwrap(raw)

    if not isinstance(track, dict):
        return None

    spotify_id = _first_string(
        track.get("id"),
        track.get("trackId"),
    )

    title = _first_string(
        track.get("name"),
        track.get("title"),
    )

    artist = _extract_artists(track)
    album = _extract_album(track)
    duration = _extract_duration(track)
    artwork = _extract_artwork(track)

    if not title and not spotify_id:
        return None

    return {
        "id": spotify_id,
        "title": title or "Unknown title",
        "artist": artist or "Unknown artist",
        "album": album or "",
        "duration": duration,
        "artwork": artwork,
        "spotify_url": (
            f"https://open.spotify.com/track/{spotify_id}"
            if spotify_id
            else ""
        ),
    }


def _recursive_find_tracks(
    obj: Any,
    results: list,
) -> None:
    """
    Recursively searches API40 response for track objects.
    """

    if isinstance(obj, dict):

        # Common direct track object
        if (
            "id" in obj
            and (
                "name" in obj
                or "title" in obj
            )
        ):
            normalised = _normalise_track(obj)

            if normalised:
                results.append(normalised)

        for value in obj.values():
            _recursive_find_tracks(value, results)

    elif isinstance(obj, list):
        for item in obj:
            _recursive_find_tracks(item, results)


# ============================================================
# SPOTIFY SEARCH
# ============================================================

def search_spotify(
    query: str,
    limit: int = MAX_SEARCH_RESULTS,
) -> list:
    """
    Search Spotify using Spotify API40.
    """

    _require_configuration()

    query = str(query or "").strip()

    if not query:
        return []

    params = {
        "query": query,
    }

    try:
        response = requests.get(
            API40_SEARCH_URL,
            headers=_api40_headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    except requests.RequestException as exc:
        raise SpotifyAPIError(
            f"Spotify search request failed: {exc}"
        ) from exc

    if response.status_code != 200:
        raise SpotifyAPIError(
            f"Spotify search returned HTTP "
            f"{response.status_code}: {response.text[:500]}"
        )

    try:
        payload = response.json()

    except ValueError as exc:
        raise SpotifyAPIError(
            "Spotify search returned invalid JSON."
        ) from exc

    results = []

    # ========================================================
    # Known Spotify API40 structure
    #
    # data.searchV2.tracksV2.items
    # ========================================================

    data = payload.get("data")

    if isinstance(data, dict):

        search_v2 = data.get("searchV2")

        if isinstance(search_v2, dict):

            tracks_v2 = search_v2.get("tracksV2")

            if isinstance(tracks_v2, dict):

                items = tracks_v2.get("items")

                if isinstance(items, list):

                    for item in items:
                        track = _normalise_track(item)

                        if track:
                            results.append(track)

    # ========================================================
    # Alternative structure
    #
    # data.tracks.items
    # ========================================================

    if not results and isinstance(data, dict):

        tracks = data.get("tracks")

        if isinstance(tracks, dict):

            items = tracks.get("items")

            if isinstance(items, list):

                for item in items:
                    track = _normalise_track(item)

                    if track:
                        results.append(track)

    # ========================================================
    # Generic fallback
    # ========================================================

    if not results:
        _recursive_find_tracks(
            payload,
            results,
        )

    # Remove duplicate Spotify IDs
    unique = []
    seen = set()

    for track in results:

        track_id = track.get("id")

        if track_id:

            if track_id in seen:
                continue

            seen.add(track_id)

        unique.append(track)

        if len(unique) >= limit:
            break

    return unique


# ============================================================
# SPOTIFY URL
# ============================================================

def extract_spotify_track_id(url: str) -> Optional[str]:
    """
    Extract Spotify track ID from:

    https://open.spotify.com/track/XXXXXXXX
    https://open.spotify.com/track/XXXXXXXX?si=...
    """

    if not url:
        return None

    try:
        parsed = urlparse(url)

        if "spotify.com" not in parsed.netloc.lower():
            return None

        match = re.search(
            r"/track/([A-Za-z0-9]+)",
            parsed.path,
        )

        if match:
            return match.group(1)

    except Exception:
        pass

    return None


# ============================================================
# GET SPOTIFY TRACK
# ============================================================

def get_spotify_track(
    spotify_id: str,
) -> Dict[str, Any]:
    """
    Fetch complete track information using Spotify API40.
    """

    _require_configuration()

    spotify_id = str(spotify_id or "").strip()

    if not spotify_id:
        raise SpotifyAPIError(
            "Spotify track ID is missing."
        )

    try:
        response = requests.get(
            API40_TRACK_URL,
            headers=_api40_headers(),
            params={
                "id": spotify_id,
            },
            timeout=REQUEST_TIMEOUT,
        )

    except requests.RequestException as exc:
        raise SpotifyAPIError(
            f"Spotify track request failed: {exc}"
        ) from exc

    if response.status_code != 200:
        raise SpotifyAPIError(
            f"Spotify track returned HTTP "
            f"{response.status_code}: {response.text[:500]}"
        )

    try:
        payload = response.json()

    except ValueError as exc:
        raise SpotifyAPIError(
            "Spotify track returned invalid JSON."
        ) from exc

    track = _normalise_track(payload)

    if not track:

        candidates = []

        _recursive_find_tracks(
            payload,
            candidates,
        )

        if candidates:
            track = candidates[0]

    if not track:
        raise SpotifyAPIError(
            "Could not extract Spotify track information."
        )

    return track


# ============================================================
# DOWNLOADER9 RESPONSE
# ============================================================

def _parse_downloader_response(
    payload: Dict[str, Any],
) -> Dict[str, Any]:

    data = payload.get("data")

    if not isinstance(data, dict):
        data = payload

    download_url = _first_string(
        data.get("downloadLink"),
        data.get("downloadUrl"),
        data.get("url"),
    )

    title = _first_string(
        data.get("title"),
        data.get("name"),
    )

    artist = _first_string(
        data.get("artist"),
        data.get("artists"),
    )

    album = _first_string(
        data.get("album"),
    )

    cover = _first_string(
        data.get("cover"),
        data.get("coverUrl"),
        data.get("image"),
    )

    if not download_url:
        raise SpotifyDownloadError(
            "Spotify Downloader9 did not return a download URL."
        )

    return {
        "download_url": download_url,
        "title": title or "Spotify Audio",
        "artist": artist or "Unknown artist",
        "album": album or "",
        "cover": cover,
    }


# ============================================================
# SPOTIFY AUDIO DOWNLOAD
# ============================================================

def download_spotify_song(
    spotify_id: str,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Download Spotify track using Downloader9.

    Flow:

        Spotify ID
            ↓
        Downloader9
            ↓
        downloadLink
            ↓
        MP3/audio file
    """

    _require_configuration()

    spotify_id = str(spotify_id or "").strip()

    if not spotify_id:
        raise SpotifyDownloadError(
            "Spotify track ID is missing."
        )

    # --------------------------------------------------------
    # Create job directory
    # --------------------------------------------------------

    timestamp = int(time.time() * 1000)

    job_dir = Path(
        output_dir
        or (
            DOWNLOAD_ROOT
            / f"spotify_{spotify_id}_{timestamp}"
        )
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        # ----------------------------------------------------
        # Ask Downloader9 for download URL
        # ----------------------------------------------------

        try:
            response = requests.get(
                DOWNLOADER_URL,
                headers=_downloader_headers(),
                params={
                    "songId": spotify_id,
                },
                timeout=REQUEST_TIMEOUT,
            )

        except requests.RequestException as exc:
            raise SpotifyDownloadError(
                f"Spotify downloader request failed: {exc}"
            ) from exc

        if response.status_code != 200:
            raise SpotifyDownloadError(
                f"Spotify downloader returned HTTP "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()

        except ValueError as exc:
            raise SpotifyDownloadError(
                "Spotify downloader returned invalid JSON."
            ) from exc

        if payload.get("success") is False:
            raise SpotifyDownloadError(
                str(
                    payload.get("message")
                    or payload.get("error")
                    or "Spotify downloader failed."
                )
            )

        download_info = _parse_downloader_response(
            payload
        )

        # ----------------------------------------------------
        # Download actual audio file
        # ----------------------------------------------------

        download_url = download_info["download_url"]

        try:
            audio_response = requests.get(
                download_url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
            )

        except requests.RequestException as exc:
            raise SpotifyDownloadError(
                f"Audio download failed: {exc}"
            ) from exc

        if audio_response.status_code != 200:
            raise SpotifyDownloadError(
                f"Audio download returned HTTP "
                f"{audio_response.status_code}"
            )

        title = download_info["title"]
        artist = download_info["artist"]

        filename = _safe_filename(
            f"{artist} - {title}"
        )

        output_path = job_dir / f"{filename}.mp3"

        # ----------------------------------------------------
        # Write file
        # ----------------------------------------------------

        with open(output_path, "wb") as file:

            for chunk in audio_response.iter_content(
                chunk_size=1024 * 1024
            ):

                if chunk:
                    file.write(chunk)

        # ----------------------------------------------------
        # Validate
        # ----------------------------------------------------

        if not output_path.exists():
            raise SpotifyDownloadError(
                "Downloaded Spotify file does not exist."
            )

        if output_path.stat().st_size < 1024:
            raise SpotifyDownloadError(
                "Downloaded Spotify file is empty or invalid."
            )

        # ----------------------------------------------------
        # Return standard structure used by bot.py
        # ----------------------------------------------------

        return {
            "path": str(output_path),
            "filename": output_path.name,
            "title": title,
            "artist": artist,
            "album": download_info["album"],
            "duration": None,
            "quality": "Spotify",
            "artwork": download_info["cover"],
            "job_dir": str(job_dir),
            "source": "spotify",
            "spotify_id": spotify_id,
        }

    except Exception:

        # If anything fails, remove partial files.
        try:
            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )
        except Exception:
            pass

        raise


# ============================================================
# CLEANUP
# ============================================================

def cleanup_spotify_job(
    result: Optional[Dict[str, Any]],
) -> None:
    """
    Remove Spotify temporary download directory.
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

    except Exception:
        pass