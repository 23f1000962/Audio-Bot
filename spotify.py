"""
Spotify / YouTube Music downloader for Audio-Bot.

Architecture:
    Spotify track URL
        -> public Spotify page metadata
        -> YouTube Music search
        -> best matching YouTube Music result
        -> yt-dlp downloads from music.youtube.com
        -> FFmpeg converts to MP3

Normal text search:
    user query -> YouTube Music search

No Spotify API.
No RapidAPI.
No YouTube Data API.

bot.py compatibility:
    search_spotify(query, limit)
    download_spotify_song(track_id, output_dir)
    cleanup_spotify_job(result)
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from ytmusicapi import YTMusic

logger = logging.getLogger(__name__)

SPOTIFY_DOWNLOADER_VERSION = "youtube-music-bgutil-cookies-lowram-1.3"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SPOTIFY_TIMEOUT = int(os.getenv("SPOTIFY_TIMEOUT", "20"))
YT_MUSIC_RESULT_LIMIT = int(os.getenv("YT_MUSIC_RESULT_LIMIT", "8"))
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "192")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
YTDLP_TIMEOUT = int(os.getenv("YTDLP_TIMEOUT", "600"))
FFPROBE_TIMEOUT = int(os.getenv("FFPROBE_TIMEOUT", "30"))
SPOTIFY_DOWNLOAD_CANDIDATES = int(
    os.getenv("SPOTIFY_DOWNLOAD_CANDIDATES", "6")
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

_SPOTIFY_TRACK_RE = re.compile(
    r"(?:https?://)?(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?track/([A-Za-z0-9]+)",
    re.IGNORECASE,
)

_SPOTIFY_URI_RE = re.compile(
    r"spotify:track:([A-Za-z0-9]+)",
    re.IGNORECASE,
)

_SPOTIFY_ANY_RE = re.compile(
    r"(?:https?://)?(?:open\.)?spotify\.com/"
    r"(?:intl-[^/]+/)?(track|album|playlist|artist)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)

_BAD_TERMS = {
    "remix": -35,
    "sped up": -40,
    "slowed": -40,
    "slowed + reverb": -45,
    "nightcore": -45,
    "8d": -45,
    "karaoke": -50,
    "instrumental": -40,
    "cover": -40,
    "reaction": -50,
    "mashup": -40,
    "lofi": -25,
}

_GOOD_TERMS = {
    "official audio": 20,
    "official": 10,
    "audio": 8,
    "topic": 8,
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    return re.sub(r"\s+", " ", value).strip()


def _safe_filename(value: str, fallback: str = "audio") -> str:
    value = _clean_text(value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:180] or fallback)


def _normalise(value: str) -> str:
    value = _clean_text(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[\(\)\[\]\{\}:,!?.'\"`]", " ", value)
    value = re.sub(r"\b(feat|ft|featuring)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _duration_seconds(value: Any) -> float | None:
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            return float(value)

        value = str(value).strip()

        if value.isdigit():
            return float(value)

        parts = value.split(":")
        if not all(part.isdigit() for part in parts):
            return None

        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])

        if len(parts) == 3:
            return (
                int(parts[0]) * 3600
                + int(parts[1]) * 60
                + int(parts[2])
            )
    except Exception:
        return None

    return None


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""

    seconds = int(seconds)

    if seconds >= 3600:
        return (
            f"{seconds // 3600}:"
            f"{(seconds % 3600) // 60:02d}:"
            f"{seconds % 60:02d}"
        )

    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------------------
# Spotify public URL / metadata
# ---------------------------------------------------------------------------

def parse_spotify_url(url: str) -> dict[str, str] | None:
    """
    Parse a Spotify URL or Spotify URI.

    Returns:
        {"type": "track", "id": "..."}
    """
    if not url:
        return None

    url = url.strip()

    match = _SPOTIFY_URI_RE.search(url)
    if match:
        return {
            "type": "track",
            "id": match.group(1),
        }

    match = _SPOTIFY_ANY_RE.search(url)
    if not match:
        return None

    return {
        "type": match.group(1).lower(),
        "id": match.group(2),
    }


def _meta_content(page: str, name: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(name)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(name)}["\']',
    ]

    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))

    return ""


def _json_ld(page: str) -> dict[str, Any]:
    pattern = (
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
        r"(.*?)</script>"
    )

    for match in re.finditer(
        pattern,
        page,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(html.unescape(match.group(1).strip()))
        except Exception:
            continue

        if isinstance(data, dict):
            return data

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item

    return {}


def _fetch_spotify_track(track_id: str) -> dict[str, Any]:
    """
    Get track metadata from Spotify's public web page.

    This does NOT use the Spotify API.
    """
    url = f"https://open.spotify.com/track/{quote(track_id)}"

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=SPOTIFY_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    page = response.text

    title = _meta_content(page, "og:title")
    description = _meta_content(page, "og:description")
    artwork = _meta_content(page, "og:image")

    data = _json_ld(page)

    if not title:
        title = _clean_text(data.get("name"))

    artist = ""
    album = ""

    author = data.get("author")

    if isinstance(author, dict):
        artist = _clean_text(author.get("name"))
    elif isinstance(author, list):
        artist = ", ".join(
            _clean_text(item.get("name"))
            for item in author
            if isinstance(item, dict) and item.get("name")
        )

    part_of = data.get("isPartOf")
    if isinstance(part_of, dict):
        album = _clean_text(part_of.get("name"))

    # Spotify's public description often looks like:
    # "Song · Artist · Album"
    if description:
        parts = [
            p.strip()
            for p in re.split(r"[·|•]", description)
            if p.strip()
        ]

        if not artist and len(parts) >= 2:
            artist = parts[1]

        if not album and len(parts) >= 3:
            album = parts[2]

    # Another common Spotify title format:
    # "Song - song and lyrics by Artist"
    if title and not artist:
        match = re.search(
            r"\s*-\s*(?:song and lyrics by|lyrics by)\s+(.+)$",
            title,
            re.IGNORECASE,
        )

        if match:
            artist = _clean_text(match.group(1))
            title = _clean_text(title[:match.start()])

    if not title:
        raise RuntimeError(
            "Spotify did not expose the track title on its public page."
        )

    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album": album,
        "artwork": artwork,
        "spotify_url": url,
    }


# ---------------------------------------------------------------------------
# YouTube Music
# ---------------------------------------------------------------------------

_YTMUSIC: YTMusic | None = None


def _get_ytmusic() -> YTMusic:
    global _YTMUSIC

    if _YTMUSIC is None:
        # Unauthenticated public YouTube Music client.
        # No API key is required.
        _YTMUSIC = YTMusic()

    return _YTMUSIC


def _artists_text(item: dict[str, Any]) -> str:
    artists = item.get("artists") or []

    if isinstance(artists, str):
        return _clean_text(artists)

    names = []

    for artist in artists:
        if isinstance(artist, dict):
            name = artist.get("name")
        else:
            name = artist

        if name:
            names.append(_clean_text(name))

    return ", ".join(names)


def _thumbnail(item: dict[str, Any]) -> str:
    thumbnails = item.get("thumbnails") or []

    if not thumbnails:
        return ""

    for thumb in reversed(thumbnails):
        if isinstance(thumb, dict) and thumb.get("url"):
            return thumb["url"]

    return ""


def _search_ytmusic(
    query: str,
    limit: int = YT_MUSIC_RESULT_LIMIT,
    filter_name: str = "songs",
) -> list[dict[str, Any]]:
    """
    Search YouTube Music itself through ytmusicapi.

    This is NOT a regular YouTube search.
    """
    query = _clean_text(query)

    if not query:
        return []

    yt = _get_ytmusic()

    try:
        results = yt.search(
            query,
            filter=filter_name,
            limit=max(1, min(limit, 20)),
        )
    except TypeError:
        results = yt.search(
            query,
            filter=filter_name,
        )

    output: list[dict[str, Any]] = []

    for item in results[:limit]:
        if not isinstance(item, dict):
            continue

        video_id = item.get("videoId")

        if not video_id:
            continue

        duration = _duration_seconds(
            item.get("duration_seconds")
            or item.get("duration")
        )

        album = item.get("album")
        if isinstance(album, dict):
            album_name = _clean_text(album.get("name"))
        else:
            album_name = _clean_text(album)

        output.append(
            {
                "id": video_id,
                "track_id": video_id,
                "youtube_id": video_id,
                "title": _clean_text(item.get("title")),
                "artist": _artists_text(item),
                "album": album_name,
                "duration": duration,
                "duration_seconds": duration,
                "duration_text": _format_duration(duration),
                "artwork": _thumbnail(item),
                "youtube_url": (
                    f"https://music.youtube.com/watch?v={video_id}"
                ),
                "source": "youtube_music",
            }
        )

    return output


def _score_result(
    result: dict[str, Any],
    wanted_title: str,
    wanted_artist: str = "",
    wanted_duration: float | None = None,
) -> float:
    result_title = _clean_text(result.get("title"))
    result_artist = _clean_text(result.get("artist"))

    title_a = _normalise(wanted_title)
    title_b = _normalise(result_title)

    artist_a = _normalise(wanted_artist)
    artist_b = _normalise(result_artist)

    score = 0.0

    if title_a and title_b:
        if title_a == title_b:
            score += 120

        wanted_words = set(title_a.split())
        result_words = set(title_b.split())

        if wanted_words:
            score += (
                len(wanted_words & result_words)
                / len(wanted_words)
            ) * 70

        if title_a in title_b or title_b in title_a:
            score += 25

    if artist_a and artist_b:
        if artist_a == artist_b:
            score += 100

        wanted_words = set(artist_a.split())
        result_words = set(artist_b.split())

        if wanted_words:
            score += (
                len(wanted_words & result_words)
                / len(wanted_words)
            ) * 60

        if artist_a in artist_b or artist_b in artist_a:
            score += 20

    result_duration = _duration_seconds(
        result.get("duration_seconds")
        or result.get("duration")
    )

    if wanted_duration and result_duration:
        difference = abs(wanted_duration - result_duration)

        if difference <= 3:
            score += 35
        elif difference <= 8:
            score += 20
        elif difference <= 15:
            score += 5
        else:
            score -= min(30, difference / 5)

    lowered = _normalise(f"{result_title} {result_artist}")

    for term, points in _BAD_TERMS.items():
        if _normalise(term) in lowered:
            score += points

    for term, points in _GOOD_TERMS.items():
        if _normalise(term) in lowered:
            score += points

    return score


def _find_ytmusic_candidates(
    title: str,
    artist: str = "",
    duration: float | None = None,
    limit: int = YT_MUSIC_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """
    Return multiple ranked YouTube Music candidates.

    The old implementation selected only the highest-scoring result. That
    meant one broken YouTube source could make an otherwise valid Spotify
    track fail completely.

    The downloader now uses this ranked list as a technical fallback chain:
        candidate 1 -> candidate 2 -> candidate 3 -> ...

    Matching remains metadata-based; technical download failures are handled
    separately by download_spotify_song().
    """
    queries: list[str] = []

    if artist:
        queries.extend(
            [
                f"{title} {artist}",
                f"{title} {artist} official audio",
                f"{title} {artist} official",
            ]
        )
    else:
        queries.append(title)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    search_limit = max(
        1,
        min(max(limit, YT_MUSIC_RESULT_LIMIT), 20),
    )

    for query in queries:
        for filter_name in ("songs", "videos"):
            try:
                results = _search_ytmusic(
                    query,
                    search_limit,
                    filter_name,
                )
            except Exception as exc:
                logger.warning(
                    "YouTube Music %s search failed for %r: %s",
                    filter_name,
                    query,
                    exc,
                )
                continue

            for result in results:
                video_id = result.get("id")

                if not video_id or video_id in seen:
                    continue

                seen.add(video_id)
                candidates.append(result)

    if not candidates:
        return []

    ranked = [
        (
            _score_result(
                result,
                title,
                artist,
                duration,
            ),
            result,
        )
        for result in candidates
    ]

    ranked.sort(
        key=lambda pair: pair[0],
        reverse=True,
    )

    output: list[dict[str, Any]] = []

    for rank, (score, result) in enumerate(
        ranked[: max(1, min(limit, 20))],
        start=1,
    ):
        enriched = dict(result)
        enriched["match_score"] = round(score, 2)
        enriched["match_rank"] = rank
        output.append(enriched)

        logger.info(
            "YouTube Music candidate #%d: score=%.1f title=%r "
            "artist=%r duration=%s id=%s",
            rank,
            score,
            result.get("title"),
            result.get("artist"),
            result.get("duration_text")
            or _format_duration(
                _duration_seconds(
                    result.get("duration_seconds")
                    or result.get("duration")
                )
            ),
            result.get("id"),
        )

    return output


def _find_best_ytmusic_result(
    title: str,
    artist: str = "",
    duration: float | None = None,
) -> dict[str, Any] | None:
    """Compatibility wrapper returning the first ranked candidate."""
    candidates = _find_ytmusic_candidates(
        title=title,
        artist=artist,
        duration=duration,
        limit=YT_MUSIC_RESULT_LIMIT,
    )

    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Public search function expected by bot.py
# ---------------------------------------------------------------------------

def search_spotify(
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    """
    Kept under the old function name so bot.py does not need to change.

    - Spotify URL -> Spotify public metadata -> YouTube Music match.
    - Normal text -> YouTube Music search.
    """
    query = _clean_text(query)

    if not query:
        return {"results": []}

    parsed = parse_spotify_url(query)

    if parsed:
        if parsed["type"] != "track":
            return {
                "results": [],
                "error": (
                    f"Spotify {parsed['type']} URLs are recognized, "
                    "but only individual tracks are currently supported."
                ),
            }

        try:
            spotify_track = _fetch_spotify_track(parsed["id"])

            candidates = _find_ytmusic_candidates(
                title=spotify_track["title"],
                artist=spotify_track["artist"],
                limit=max(1, min(limit, 20)),
            )

            if not candidates:
                return {
                    "results": [],
                    "error": (
                        "The Spotify track was found, but no matching "
                        "YouTube Music song was found."
                    ),
                }

            results = []

            for candidate in candidates:
                enriched = dict(candidate)
                enriched.update(
                    {
                        "spotify_id": parsed["id"],
                        "spotify_url": spotify_track["spotify_url"],
                        "album": (
                            spotify_track["album"]
                            or candidate.get("album", "")
                        ),
                        "artwork": (
                            candidate.get("artwork")
                            or spotify_track.get("artwork")
                            or ""
                        ),
                        "source": "youtube_music",
                    }
                )
                results.append(enriched)

            return {"results": results}

        except Exception as exc:
            logger.exception("Spotify track lookup failed")

            return {
                "results": [],
                "error": f"Could not resolve Spotify track: {exc}",
            }

    try:
        results = _search_ytmusic(
            query,
            max(1, min(limit, 20)),
            "songs",
        )

        return {"results": results}

    except Exception as exc:
        logger.exception("YouTube Music search failed")

        return {
            "results": [],
            "error": f"YouTube Music search failed: {exc}",
        }


# ---------------------------------------------------------------------------
# yt-dlp authentication / PO-token configuration
# ---------------------------------------------------------------------------

def _find_cookie_source() -> Path | None:
    """
    Find the source cookies.txt.

    Render Secret Files are mounted read-only under /etc/secrets/.
    yt-dlp may write/update a Netscape cookie jar when it exits, so the
    source file must NOT be passed directly to yt-dlp on Render.
    """
    configured = os.getenv("YT_DLP_COOKIES") or os.getenv("COOKIES_FILE")

    candidates = []

    if configured:
        candidates.append(Path(configured))

    candidates.extend(
        [
            Path("/etc/secrets/cookies.txt"),
            Path("/app/cookies.txt"),
            Path("cookies.txt"),
        ]
    )

    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue

    return None


def _is_valid_cookie_file(path: Path) -> bool:
    """
    Validate a Netscape/Mozilla cookie jar before yt-dlp uses it.

    This rejects common Render mistakes such as putting:
        RENDER_EXTERNAL_URL=...
    inside cookies.txt.
    """
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False

        lines = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()

        header_found = any(
            line.strip().startswith("# Netscape HTTP Cookie File")
            or line.strip().startswith("# HTTP Cookie File")
            for line in lines[:20]
        )

        if not header_found:
            return False

        for line in lines:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            # Netscape cookies have exactly seven TAB-separated fields.
            if len(line.split("\t")) != 7:
                return False

        return True

    except (OSError, UnicodeError):
        return False


def _find_cookie_file() -> str | None:
    """
    Return a validated writable copy of cookies.txt.

    Render Secret Files are read-only, so yt-dlp gets a /tmp copy.
    Any stale invalid runtime copy is removed first.
    """
    source = _find_cookie_source()
    writable = Path("/tmp/audio-bot-cookies.txt")

    if source is None:
        try:
            if writable.exists():
                writable.unlink()
        except OSError:
            pass
        return None

    try:
        if not _is_valid_cookie_file(source):
            logger.error(
                "Invalid cookies.txt at %s. Expected a Netscape-format "
                "browser cookie export. Do NOT put environment variables "
                "such as RENDER_EXTERNAL_URL in cookies.txt.",
                source,
            )

            try:
                if writable.exists():
                    writable.unlink()
            except OSError:
                pass

            return None

        writable.write_bytes(source.read_bytes())
        os.chmod(writable, 0o600)

        if not _is_valid_cookie_file(writable):
            logger.error(
                "Runtime cookie copy failed validation; ignoring cookies."
            )
            try:
                writable.unlink()
            except OSError:
                pass
            return None

        logger.info(
            "Using validated writable yt-dlp cookie copy: %s "
            "(source: %s)",
            writable,
            source,
        )

        return str(writable)

    except Exception as exc:
        logger.warning(
            "Could not prepare cookies.txt from %s: %s",
            source,
            exc,
        )

        try:
            if writable.exists():
                writable.unlink()
        except OSError:
            pass

        return None


def _ytdlp_common_args() -> list[str]:
    """
    Arguments shared by metadata and download calls.

    BGUTIL runs as a separate Render service. The explicit remote HTTP
    endpoint makes the connection unambiguous.

    A cookies.txt is added automatically when present.
    """
    args = [
        "--no-playlist",
        "--no-warnings",
        "--extractor-args",
        "youtubepot-bgutilhttp:base_url=https://audiobot-bgutil.onrender.com",
    ]

    cookie_file = _find_cookie_file()

    if cookie_file:
        logger.info("Using yt-dlp cookies file: %s", cookie_file)
        args.extend(["--cookies", cookie_file])
    else:
        logger.warning(
            "No cookies.txt found. YouTube may reject the download "
            "with a bot/authentication challenge."
        )

    return args


def _check_ytdlp() -> None:
    try:
        process = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        if process.returncode != 0:
            raise RuntimeError(
                process.stderr.strip() or "yt-dlp is unavailable."
            )

        logger.info("yt-dlp version: %s", process.stdout.strip())

    except FileNotFoundError as exc:
        raise RuntimeError(
            "yt-dlp is not installed or is not available in PATH."
        ) from exc


# ---------------------------------------------------------------------------
# Exact YouTube Music metadata
# ---------------------------------------------------------------------------

def _get_ytdlp_metadata(video_id: str) -> dict[str, Any]:
    url = f"https://music.youtube.com/watch?v={video_id}"

    _check_ytdlp()

    command = [
        "yt-dlp",
        *_ytdlp_common_args(),
        "--skip-download",
        "--dump-single-json",
        url,
    ]

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=YTDLP_TIMEOUT,
        check=False,
    )

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "yt-dlp could not read the YouTube Music video."
        )

    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "yt-dlp returned invalid metadata."
        ) from exc


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _run_ffprobe(audio_path: Path) -> dict[str, Any]:
    """
    Inspect the first audio stream using ffprobe.

    A successful yt-dlp process is not enough: for problematic YouTube
    sources, yt-dlp can leave a file that exists but has no readable audio
    stream. ffprobe is the authoritative check before accepting a candidate.
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,duration",
        "-of",
        "json",
        str(audio_path),
    ]

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffprobe is not installed or is not available in PATH."
        ) from exc

    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.strip()
            or "ffprobe could not inspect the audio file."
        )

    try:
        data = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "ffprobe returned invalid JSON."
        ) from exc

    streams = data.get("streams") or []

    if not streams:
        raise RuntimeError(
            "ffprobe found no audio stream in the downloaded file."
        )

    stream = streams[0] if isinstance(streams[0], dict) else {}
    codec = _clean_text(stream.get("codec_name"))

    if not codec:
        raise RuntimeError(
            "ffprobe could not determine the audio codec."
        )

    duration = _duration_seconds(stream.get("duration"))

    return {
        "codec": codec,
        "duration": duration,
    }


def _audio_files(job_dir: Path) -> list[Path]:
    return [
        path
        for path in job_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {
            ".mp3",
            ".m4a",
            ".opus",
            ".webm",
        }
    ]


def _find_valid_mp3(job_dir: Path) -> tuple[Path, dict[str, Any]]:
    """
    Find the newest MP3 and require a valid audio stream.
    """
    audio_files = [
        path
        for path in _audio_files(job_dir)
        if path.suffix.lower() == ".mp3"
    ]

    if not audio_files:
        raise RuntimeError(
            "yt-dlp completed but no MP3 file was produced."
        )

    audio_path = max(
        audio_files,
        key=lambda path: path.stat().st_mtime,
    )

    if audio_path.stat().st_size <= 0:
        raise RuntimeError("The downloaded MP3 is empty.")

    probe = _run_ffprobe(audio_path)

    return audio_path, probe


def _is_authentication_error(message: str) -> bool:
    lowered = message.lower()

    return any(
        marker in lowered
        for marker in (
            "sign in to confirm",
            "not a bot",
            "cookies-from-browser",
            "authentication challenge",
            "confirm you're not a bot",
        )
    )


def _is_audio_codec_failure(message: str) -> bool:
    lowered = message.lower()

    return any(
        marker in lowered
        for marker in (
            "unable to obtain file audio codec with ffprobe",
            "could not determine the audio codec",
            "no audio stream",
            "ffprobe found no audio stream",
        )
    )


def _is_postprocessing_failure(message: str) -> bool:
    lowered = message.lower()

    return any(
        marker in lowered
        for marker in (
            "postprocessing",
            "conversion failed",
            "embedthumbnail",
            "unable to embed",
            "thumbnail",
        )
    )


def _build_spotify_ytdlp_command(
    music_url: str,
    output_template: str,
    embed_thumbnail: bool = True,
) -> list[str]:
    command = [
        "yt-dlp",
        *_ytdlp_common_args(),
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "--no-check-certificates",
        "--format",
        "bestaudio/best",
        "--concurrent-fragments",
        "1",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--socket-timeout",
        "30",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        f"{AUDIO_QUALITY}K",
        "--add-metadata",
    ]

    if embed_thumbnail:
        command.extend(
            [
                "--embed-thumbnail",
                "--postprocessor-args",
                "ThumbnailsConvertor:-q:v 2",
            ]
        )

    command.extend(
        [
            "--output",
            output_template,
            music_url,
        ]
    )

    return command


def _download_one_spotify_candidate(
    video_id: str,
    title: str,
    artist: str,
    job_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """
    Download one YouTube Music candidate.

    Returns:
        (validated_mp3_path, ffprobe_metadata)

    The function performs a secondary retry without thumbnail only for
    genuine postprocessing/thumbnail failures. An ffprobe/audio-codec
    failure is treated as a bad candidate and returned to the caller so the
    next candidate can be tried.
    """
    candidate_dir = job_dir / f"candidate_{video_id}"
    candidate_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    base_name = _safe_filename(
        f"{title} - {artist}" if artist else title,
        "audio",
    )

    output_template = str(
        candidate_dir / f"{base_name}.%(ext)s"
    )

    music_url = (
        f"https://music.youtube.com/watch?v={video_id}"
    )

    def run_download(embed_thumbnail: bool) -> subprocess.CompletedProcess:
        command = _build_spotify_ytdlp_command(
            music_url=music_url,
            output_template=output_template,
            embed_thumbnail=embed_thumbnail,
        )

        logger.info(
            "Downloading YouTube Music candidate %s "
            "(thumbnail=%s)",
            video_id,
            embed_thumbnail,
        )

        return subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=YTDLP_TIMEOUT,
            check=False,
        )

    process = run_download(embed_thumbnail=True)

    if process.returncode != 0:
        message = process.stderr.strip()

        if _is_authentication_error(message):
            raise RuntimeError(
                "YouTube rejected the request as a bot/authentication "
                "challenge. Make sure a fresh Netscape-format "
                "cookies.txt is available at /etc/secrets/cookies.txt "
                "on Render. Do NOT put environment variables in "
                "cookies.txt. Original yt-dlp error:\n"
                + (message[-3000:] or "authentication challenge")
            )

        # If yt-dlp reports the ffprobe/audio-codec problem, do NOT waste
        # another attempt on the same broken source. The caller will move
        # directly to the next YouTube Music candidate.
        if _is_audio_codec_failure(message):
            raise RuntimeError(
                "Candidate has no readable audio stream. "
                "ffprobe/codec validation failed:\n"
                + (message[-1500:] or "unknown codec failure")
            )

        if _is_postprocessing_failure(message):
            logger.warning(
                "YouTube Music postprocessing failed for %s; "
                "retrying without thumbnail.",
                video_id,
            )

            process = run_download(embed_thumbnail=False)

            if process.returncode != 0:
                message = process.stderr.strip()

                if _is_authentication_error(message):
                    raise RuntimeError(
                        "YouTube rejected the request as a "
                        "bot/authentication challenge. Original "
                        "yt-dlp error:\n"
                        + (message[-3000:] or "authentication challenge")
                    )

                if _is_audio_codec_failure(message):
                    raise RuntimeError(
                        "Candidate has no readable audio stream after "
                        "thumbnail fallback. ffprobe/codec validation "
                        "failed:\n"
                        + (message[-1500:] or "unknown codec failure")
                    )

        if process.returncode != 0:
            message = process.stderr.strip()

            raise RuntimeError(
                message[-3000:]
                if message
                else "yt-dlp failed to download this YouTube Music candidate."
            )

    # Even when yt-dlp exits successfully, validate the resulting MP3.
    return _find_valid_mp3(candidate_dir)


def download_spotify_song(
    track_id: str,
    output_dir: str | Path,
    track: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Download Spotify/YouTube Music audio with ranked technical fallbacks.

    Strategy:
        1. Put the explicitly selected YouTube Music result first.
        2. Search additional ranked candidates for the same title/artist.
        3. Try candidates sequentially.
        4. Validate every resulting MP3 with ffprobe.
        5. If thumbnail postprocessing fails, retry that same candidate
           without thumbnail.
        6. If the candidate has no readable audio stream, discard it and
           immediately try the next candidate.
        7. Stop immediately for authentication failures because changing
           candidates cannot fix an invalid YouTube session.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_spotify_id = ""
    selected_video_id = ""
    title = ""
    artist = ""
    album = ""
    artwork = ""
    wanted_duration: float | None = None

    parsed = parse_spotify_url(track_id)

    if parsed:
        if parsed["type"] != "track":
            raise RuntimeError(
                "Only individual Spotify tracks are supported."
            )

        source_spotify_id = parsed["id"]
        spotify_track = _fetch_spotify_track(
            source_spotify_id
        )

        title = spotify_track["title"]
        artist = spotify_track["artist"]
        album = spotify_track["album"]
        artwork = spotify_track["artwork"]

    else:
        selected_video_id = str(
            track_id
        ).strip()

        if not re.fullmatch(
            r"[A-Za-z0-9_-]{6,20}",
            selected_video_id,
        ):
            raise RuntimeError(
                "Invalid YouTube Music video ID."
            )

        track = (
            track
            if isinstance(track, dict)
            else {}
        )

        title = _clean_text(
            track.get("title")
        )
        artist = _clean_text(
            track.get("artist")
        )
        album = _clean_text(
            track.get("album")
        )
        artwork = _clean_text(
            track.get("artwork")
        )

        wanted_duration = _duration_seconds(
            track.get("duration_seconds")
            or track.get("duration")
        )

        if not title:
            metadata = _get_ytdlp_metadata(
                selected_video_id
            )

            title = (
                _clean_text(metadata.get("track"))
                or _clean_text(metadata.get("title"))
            )

            artist = (
                artist
                or _clean_text(metadata.get("artist"))
                or _clean_text(metadata.get("uploader"))
                or _clean_text(metadata.get("channel"))
            )

            album = (
                album
                or _clean_text(metadata.get("album"))
            )

            artwork = (
                artwork
                or _clean_text(metadata.get("thumbnail"))
            )

            wanted_duration = (
                wanted_duration
                or _duration_seconds(
                    metadata.get("duration")
                )
            )

        if not title:
            raise RuntimeError(
                "Could not determine the YouTube Music track title."
            )

    # Build the ranked candidate list.
    candidates = _find_ytmusic_candidates(
        title=title,
        artist=artist,
        duration=wanted_duration,
        limit=max(
            1,
            min(
                SPOTIFY_DOWNLOAD_CANDIDATES,
                12,
            ),
        ),
    )

    # If the user selected a specific result, always try that exact result
    # first. This preserves the confirmation-selection behavior in bot.py.
    if selected_video_id:
        selected_candidate = None
        remaining = []

        for candidate in candidates:
            if candidate.get("id") == selected_video_id:
                selected_candidate = candidate
            else:
                remaining.append(candidate)

        if selected_candidate is None:
            selected_candidate = {
                "id": selected_video_id,
                "track_id": selected_video_id,
                "youtube_id": selected_video_id,
                "title": title,
                "artist": artist,
                "album": album,
                "duration": wanted_duration,
                "duration_seconds": wanted_duration,
                "duration_text": _format_duration(
                    wanted_duration
                ),
                "artwork": artwork,
                "source": "youtube_music",
            }

        candidates = [
            selected_candidate,
            *remaining,
        ]

    if not candidates:
        raise RuntimeError(
            "Could not find any usable YouTube Music candidates "
            f"for {title!r}."
        )

    # Deduplicate candidate IDs while preserving order.
    unique_candidates = []
    seen_ids: set[str] = set()

    for candidate in candidates:
        video_id = str(
            candidate.get("id")
            or candidate.get("video_id")
            or ""
        ).strip()

        if not video_id or video_id in seen_ids:
            continue

        seen_ids.add(video_id)
        unique_candidates.append(
            candidate
        )

    job_dir = Path(
        tempfile.mkdtemp(
            prefix="spotify_",
            dir=str(output_dir),
        )
    )

    failures: list[str] = []

    try:
        for index, candidate in enumerate(
            unique_candidates,
            start=1,
        ):
            video_id = str(
                candidate.get("id")
                or candidate.get("video_id")
                or ""
            ).strip()

            candidate_title = (
                _clean_text(
                    candidate.get("title")
                )
                or title
            )

            candidate_artist = (
                _clean_text(
                    candidate.get("artist")
                )
                or artist
            )

            candidate_duration = _duration_seconds(
                candidate.get("duration_seconds")
                or candidate.get("duration")
            )

            logger.info(
                "Spotify/YT Music fallback attempt %d/%d: "
                "title=%r artist=%r duration=%s id=%s",
                index,
                len(unique_candidates),
                candidate_title,
                candidate_artist,
                _format_duration(
                    candidate_duration
                ),
                video_id,
            )

            try:
                audio_path, probe = (
                    _download_one_spotify_candidate(
                        video_id=video_id,
                        title=candidate_title,
                        artist=candidate_artist,
                        job_dir=job_dir,
                    )
                )

                file_size = audio_path.stat().st_size
                max_bytes = (
                    MAX_FILE_SIZE_MB
                    * 1024
                    * 1024
                )

                if file_size > max_bytes:
                    raise RuntimeError(
                        "The downloaded MP3 is too large for Telegram "
                        f"({file_size / 1024 / 1024:.1f} MB)."
                    )

                logger.info(
                    "Spotify download succeeded on candidate %d/%d: "
                    "%s codec=%s duration=%s",
                    index,
                    len(unique_candidates),
                    video_id,
                    probe.get("codec"),
                    _format_duration(
                        probe.get("duration")
                    ),
                )

                return {
                    "path": str(audio_path),
                    "filename": audio_path.name,
                    "title": title,
                    "artist": artist or candidate_artist,
                    "album": album or _clean_text(
                        candidate.get("album")
                    ),
                    "duration": (
                        wanted_duration
                        or candidate_duration
                        or probe.get("duration")
                    ),
                    "quality": f"MP3 {AUDIO_QUALITY} kbps",
                    "artwork": (
                        artwork
                        or _clean_text(
                            candidate.get("artwork")
                        )
                    ),
                    "job_dir": str(job_dir),
                    "source": "youtube_music",
                    "youtube_url": (
                        f"https://music.youtube.com/watch?v={video_id}"
                    ),
                    "spotify_id": source_spotify_id,
                    "youtube_id": video_id,
                    "file_size": file_size,
                    "codec": probe.get("codec"),
                    "candidate_rank": index,
                    "candidate_count": len(unique_candidates),
                }

            except Exception as exc:
                message = str(exc).strip()

                # Authentication is global, not candidate-specific.
                if _is_authentication_error(
                    message
                ):
                    raise

                failures.append(
                    f"#{index} {video_id}: "
                    f"{message[-500:] or 'unknown failure'}"
                )

                logger.warning(
                    "Spotify candidate %d/%d failed: %s",
                    index,
                    len(unique_candidates),
                    message[-1200:],
                )

                # Candidate directory is disposable. This keeps broken
                # .webm/.m4a files from contaminating later attempts.
                candidate_dir = (
                    job_dir / f"candidate_{video_id}"
                )

                shutil.rmtree(
                    candidate_dir,
                    ignore_errors=True,
                )

                continue

        summary = "\n".join(
            failures[-6:]
        )

        raise RuntimeError(
            "No usable YouTube Music audio source was found "
            f"for {title!r} after trying "
            f"{len(unique_candidates)} candidate(s).\n"
            + summary
        )

    except Exception:
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )
        raise


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_spotify_job(result: dict[str, Any] | None) -> None:
    """
    Remove temporary Spotify/YouTube Music download files.
    """
    if not result:
        return

    job_dir = result.get("job_dir")

    if not job_dir:
        return

    try:
        shutil.rmtree(str(job_dir), ignore_errors=True)
    except Exception as exc:
        logger.warning(
            "Could not clean Spotify job directory %s: %s",
            job_dir,
            exc,
        )
