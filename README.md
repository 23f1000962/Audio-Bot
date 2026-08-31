# Telegram YouTube Audio Bot

**Model version: 0.2.0**

Telegram bot that accepts YouTube video links, downloads audio, converts it to MP3, and sends it back.

## v0.2.0 changes
- `Hey`, `Hello`, `Hii`, `Hi` health-check replies
- Every health/error/status response shows the bot model version
- Broader YouTube URL matching
- No artificial source-media download-size cap
- Better yt-dlp YouTube support with `yt-dlp[default]` and Node.js
- Longer Telegram HTTP timeouts for larger uploads
- Explicit final MP3 size check with a safe Telegram upload ceiling
- Automatic cleanup after every request and at startup

## Deployment
The Dockerfile installs FFmpeg and Node.js. Set `BOT_TOKEN` in your hosting provider's environment variables and deploy the `main` branch.

Send `/start`, then send a YouTube URL.

> Telegram's infrastructure still imposes upload limits. The bot therefore checks the final MP3 size before uploading.
