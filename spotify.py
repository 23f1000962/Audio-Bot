"""Spotify metadata resolver + YouTube Music downloader using YouTube.js.

No Spotify API, RapidAPI, yt-dlp or YouTube Data API is required.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from ytmusicapi import YTMusic

ENGINE_URL = 'http://127.0.0.1:8765'
AUDIO_QUALITY = '192'
MAX_FILE_SIZE_MB = 49
SPOTIFY_TIMEOUT = 20
YT_MUSIC_RESULT_LIMIT = 8


def _clean_text(v: Any) -> str:
    return re.sub(r'\s+', ' ', html.unescape(str(v or ''))).strip()


def _duration_seconds(v: Any) -> int | None:
    try:
        n = int(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _format_duration(seconds):
    s = _duration_seconds(seconds)
    if not s:
        return ''
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m}:{sec:02d}'


def parse_spotify_url(url: str) -> dict[str, str] | None:
    m = re.search(r'(?:https?://)?(?:open\.)?spotify\.com/(?:intl-[^/]+/)?(track|album|playlist|artist)/([A-Za-z0-9]+)', url or '', re.I)
    if m:
        return {'type': m.group(1).lower(), 'id': m.group(2)}
    m = re.search(r'spotify:track:([A-Za-z0-9]+)', url or '', re.I)
    if m:
        return {'type': 'track', 'id': m.group(1)}
    return None


def _meta_content(page: str, prop: str) -> str:
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',
    ]
    for p in patterns:
        m = re.search(p, page, re.I)
        if m:
            return html.unescape(m.group(1))
    return ''


def _fetch_spotify_track(track_id: str) -> dict[str, Any]:
    url = f'https://open.spotify.com/track/{track_id}'
    r = requests.get(url, timeout=SPOTIFY_TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    page = r.text
    title = _meta_content(page, 'og:title')
    description = _meta_content(page, 'og:description')
    duration = None
    # Spotify's page metadata varies. Title and description are enough to build
    # a reliable YouTube Music search; duration is optional.
    artist = description.split('·')[0].strip() if description else ''
    if not title:
        raise RuntimeError('Could not read Spotify track metadata.')
    return {'id': track_id, 'title': title, 'artist': artist, 'album': '', 'duration': duration}


def _get_ytmusic() -> YTMusic:
    return YTMusic()


def _artists_text(item: dict[str, Any]) -> str:
    artists = item.get('artists') or []
    return ', '.join(_clean_text(a.get('name')) for a in artists if isinstance(a, dict))


def _thumbnail(item: dict[str, Any]) -> str:
    thumbs = item.get('thumbnails') or []
    return thumbs[-1].get('url', '') if thumbs and isinstance(thumbs[-1], dict) else ''


def _search_ytmusic(title: str, artist: str, limit: int = 8):
    query = f'{title} {artist}'.strip()
    return _get_ytmusic().search(query, filter='songs', limit=limit)


def _score_result(item: dict[str, Any], title: str, artist: str) -> float:
    rt = _clean_text(item.get('title')).lower()
    ra = _artists_text(item).lower()
    t = _clean_text(title).lower()
    a = _clean_text(artist).lower()
    score = 0
    if rt == t: score += 100
    elif t and t in rt: score += 55
    if a and a in ra: score += 50
    if rt and 'official audio' in rt: score += 10
    if any(x in rt for x in ('remix', 'sped up', 'slowed', 'nightcore', 'karaoke', 'cover')): score -= 30
    return score


def _find_ytmusic_candidates(title: str, artist: str, limit: int = 8):
    results = _search_ytmusic(title, artist, limit)
    normalized = []
    for item in results or []:
        if not item.get('videoId'):
            continue
        duration = item.get('duration_seconds')
        normalized.append({
            'video_id': item['videoId'],
            'title': _clean_text(item.get('title')),
            'artist': _artists_text(item),
            'album': _clean_text((item.get('album') or {}).get('name') if isinstance(item.get('album'), dict) else item.get('album')),
            'duration_seconds': duration,
            'duration_text': _format_duration(duration),
            'thumbnail': _thumbnail(item),
            'score': _score_result(item, title, artist),
        })
    normalized.sort(key=lambda x: x['score'], reverse=True)
    return normalized


def _find_best_ytmusic_result(title: str, artist: str):
    candidates = _find_ytmusic_candidates(title, artist, YT_MUSIC_RESULT_LIMIT)
    return candidates[0] if candidates else None


def search_spotify(query: str, limit: int = 8) -> dict[str, Any]:
    candidates = _find_ytmusic_candidates(query, '', limit)
    return {'query': query, 'results': candidates[:limit]}


def _engine_download(video_id: str, raw: Path) -> dict[str, Any]:
    r = requests.post(
        ENGINE_URL + '/download',
        json={'input': video_id, 'output_path': str(raw)},
        timeout=900,
    )
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(data.get('error') or 'YouTube.js download failed.')
    return data


def _convert(raw: Path, output: Path, title: str, artist: str, album: str = ''):
    command = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-i', str(raw), '-vn', '-map_metadata', '-1',
        '-codec:a', 'libmp3lame', '-b:a', f'{AUDIO_QUALITY}k',
        '-metadata', f'title={title[:200]}',
    ]
    if artist:
        command += ['-metadata', f'artist={artist[:150]}']
    if album:
        command += ['-metadata', f'album={album[:150]}']
    command.append(str(output))
    p = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or 'FFmpeg conversion failed.')


def download_spotify_song(track_id: str, output_dir: str | Path, track: dict[str, Any] | None = None) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    parsed = parse_spotify_url(track_id)
    if parsed:
        if parsed['type'] != 'track':
            raise RuntimeError('Only individual Spotify tracks are supported.')
        spotify = _fetch_spotify_track(parsed['id'])
        selected = _find_best_ytmusic_result(spotify['title'], spotify['artist'])
        if not selected:
            raise RuntimeError('Could not find the Spotify track on YouTube Music.')
        track = {**spotify, **selected}
        video_id = selected['video_id']
    else:
        track = track or {}
        video_id = track.get('video_id') or track.get('youtube_id') or track_id

    title = _clean_text(track.get('title')) or 'Audio'
    artist = _clean_text(track.get('artist'))
    album = _clean_text(track.get('album'))
    duration = track.get('duration_seconds') or track.get('duration')

    job_dir = output_dir / f'spotify-{uuid.uuid4().hex}'
    job_dir.mkdir(parents=True, exist_ok=True)
    raw = job_dir / 'source.audio'
    mp3 = job_dir / f'{re.sub(r"[\\/:*?\"<>|]+", " ", title)[:180].strip() or "Audio"}.mp3'

    result = _engine_download(video_id, raw)
    duration = result.get('duration') or duration
    title = result.get('title') or title
    artist = result.get('artist') or artist
    _convert(raw, mp3, title, artist, album)
    raw.unlink(missing_ok=True)

    size = mp3.stat().st_size
    if size > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise RuntimeError('Audio exceeds the Telegram upload limit.')

    return {
        'path': str(mp3),
        'job_dir': str(job_dir),
        'title': title,
        'artist': artist,
        'album': album,
        'duration': duration,
        'filename': mp3.name,
        'quality': f'MP3 • {AUDIO_QUALITY} kbps',
        'artwork': track.get('thumbnail') if track else None,
    }


def cleanup_spotify_job(result: dict[str, Any] | None) -> None:
    if not result:
        return
    job_dir = result.get('job_dir')
    if job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)
