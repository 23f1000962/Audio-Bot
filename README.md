# Telegram YouTube Audio Bot

A Telegram bot that accepts a YouTube video link, downloads the audio, converts it to MP3, and sends it back to the user.

## Features

- Live download progress updates
- Audio extraction to MP3
- No application-imposed file-size limit
- Automatic cleanup after every request
- Startup cleanup for files left by crashes
- Playlists are explicitly disabled
- Simple environment-variable configuration

> Telegram and hosting infrastructure may still impose their own upload or storage limits.

## Requirements

- Python 3.10+
- FFmpeg
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/23f1000962/Audio-Bot.git
cd Audio-Bot
```

### 2. Install FFmpeg

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install ffmpeg
```

### 3. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure your bot token

```bash
export BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
```

Windows PowerShell:

```powershell
$env:BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
```

### 6. Run

```bash
python bot.py
```

## Usage

Send `/start` to the bot and then send a supported YouTube video URL.

Please use the bot only for media you own or are authorized to download, and comply with the terms of the relevant platform and applicable copyright laws.
