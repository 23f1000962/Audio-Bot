"""Low-RAM YouTube audio downloader using the local YouTube.js engine."""
from __future__ import annotations
import inspect
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable
import requests

ENGINE_URL=os.getenv('YOUTUBE_ENGINE_URL','http://127.0.0.1:8765').rstrip('/')
MAX_FILE_SIZE_MB=int(os.getenv('MAX_FILE_SIZE_MB','49'))
MAX_DURATION=int(os.getenv('MAX_DURATION','7200'))
AUDIO_QUALITY=os.getenv('AUDIO_QUALITY','192')
DOWNLOAD_DIR=Path(os.getenv('DOWNLOAD_DIR','/tmp/audio-bot-downloads'))
DOWNLOAD_DIR.mkdir(parents=True,exist_ok=True)

class DownloadError(Exception): pass
class FileTooLargeError(DownloadError): pass
class DurationTooLongError(DownloadError): pass
class VideoUnavailableError(DownloadError): pass

def _engine(path:str,payload:dict|None=None)->dict:
    try:r=requests.post(ENGINE_URL+path,json=payload or {},timeout=900)
    except requests.RequestException as exc:raise DownloadError(f'YouTube engine unavailable: {exc}') from exc
    try:data=r.json()
    except json.JSONDecodeError as exc:raise DownloadError('YouTube engine returned invalid JSON.') from exc
    if r.status_code>=400:raise DownloadError(data.get('error') or f'YouTube engine HTTP {r.status_code}')
    return data

def sanitize_filename(value:str,fallback:str='audio')->str:
    value=re.sub(r'[\\/:*?"<>|\x00-\x1f]+',' ',value or '').strip();value=re.sub(r'\s+',' ',value);return value[:180] or fallback

def ensure_ffmpeg():
    if not shutil.which('ffmpeg'):raise DownloadError('FFmpeg is not installed.')

def format_bytes(value:int|float)->str:
    value=float(value or 0)
    for unit in ('B','KB','MB','GB'):
        if value<1024 or unit=='GB':return f'{value:.1f} {unit}'
        value/=1024
    return f'{value:.1f} GB'

def format_duration(seconds)->str:
    try:seconds=int(float(seconds))
    except (TypeError,ValueError):return ''
    if seconds<=0:return ''
    h,rem=divmod(seconds,3600);m,s=divmod(rem,60);return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def cookies_available()->bool:
    p=Path(os.getenv('YOUTUBE_COOKIE_FILE','/app/cookies.txt'))
    return bool(os.getenv('YOUTUBE_COOKIE') or os.getenv('YOUTUBE_COOKIES') or p.exists())

def get_source_quality(info:dict)->str:return info.get('quality') or 'YouTube.js audio'
def get_audio_quality(info:dict)->str:return info.get('quality') or f'MP3 • {AUDIO_QUALITY} kbps'

class ProgressTracker:
    def __init__(self,callback:Callable[[dict],None]|None=None):self.callback=callback
    async def update(self,status:str,percent:float|None=None,**extra):
        if not self.callback:return
        data={'status':status,'percent':percent};data.update(extra)
        result=self.callback(data)
        if inspect.isawaitable(result):await result

def duration_filter(info_dict,*,incomplete=False):
    duration=info_dict.get('duration') if isinstance(info_dict,dict) else None
    return f'Duration exceeds {MAX_DURATION} seconds.' if duration and duration>MAX_DURATION else None

def find_audio_file(job_dir:str|Path)->Path|None:
    p=Path(job_dir);files=[x for x in p.iterdir() if x.is_file()] if p.exists() else []
    return max(files,key=lambda x:x.stat().st_mtime) if files else None

def _convert_to_mp3(raw:Path,output:Path,title:str,artist:str):
    ensure_ffmpeg();command=['ffmpeg','-y','-hide_banner','-loglevel','error','-i',str(raw),'-vn','-map_metadata','-1','-codec:a','libmp3lame','-b:a',f'{AUDIO_QUALITY}k','-metadata',f'title={title[:200]}']
    if artist:command+=['-metadata',f'artist={artist[:150]}']
    command.append(str(output));p=subprocess.run(command,capture_output=True,text=True,timeout=900)
    if p.returncode!=0:raise DownloadError(p.stderr.strip() or 'FFmpeg conversion failed.')

def download_with_client(url:str,output_dir:str|Path,progress_tracker=None,**kwargs):
    return download_audio(url,output_dir,progress_callback=(progress_tracker.update if progress_tracker else None))

async def download_audio(url:str,output_dir:str|Path,progress_callback=None)->dict:
    output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=True);job_dir=output_dir/f'job-{uuid.uuid4().hex}';job_dir.mkdir(parents=True,exist_ok=True);raw=job_dir/'source.audio';tracker=ProgressTracker(progress_callback)
    await tracker.update('Connecting')
    try:
        result=_engine('/download',{'input':url,'output_path':str(raw)});duration=result.get('duration')
        if duration and duration>MAX_DURATION:raise DurationTooLongError(f'This audio is longer than {MAX_DURATION//60} minutes.')
        await tracker.update('Downloaded',70);title=result.get('title') or 'Audio';artist=result.get('artist') or '';mp3=job_dir/f'{sanitize_filename(title)}.mp3';_convert_to_mp3(raw,mp3,title,artist);raw.unlink(missing_ok=True);size=mp3.stat().st_size
        if size>MAX_FILE_SIZE_MB*1024*1024:raise FileTooLargeError(f'Audio is {format_bytes(size)}, above the {MAX_FILE_SIZE_MB} MB Telegram limit.')
        if size<=0:raise DownloadError('Downloaded audio is empty.')
        await tracker.update('Ready',100)
        return {'path':str(mp3),'job_dir':str(job_dir),'title':title,'artist':artist,'uploader':artist,'duration':duration,'filename':mp3.name,'quality':f'MP3 • {AUDIO_QUALITY} kbps','quality_text':f'MP3 • {AUDIO_QUALITY} kbps','source_url':url}
    except DownloadError:raise
    except json.JSONDecodeError as exc:raise DownloadError('YouTube engine returned invalid JSON.') from exc

def cleanup_job_directory(job_dir:str|Path|None):
    if job_dir:
        try:shutil.rmtree(job_dir,ignore_errors=True)
        except Exception:pass
