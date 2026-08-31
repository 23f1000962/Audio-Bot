# Telegram YouTube Audio Bot — v0.3.0

Telegram bot that downloads authorized YouTube media, extracts audio with FFmpeg/yt-dlp, and sends an MP3 back to Telegram.

## Version check

Send any of:

- `Hey`
- `Hello`
- `Hi`
- `Hii`

Expected response:

`👋 Hey! Audio Bot v0.3.0 is online and running.`

You can also send `/version`.

## Cloudflare Containers

This repository is configured for Cloudflare Containers.

Cloudflare deploy command:

```bash
npx wrangler deploy
```

Set `BOT_TOKEN` as a Cloudflare Worker secret before/after deployment.

The container exposes port 8080 for Cloudflare health/routing while the Telegram bot uses long polling.

## Notes

- FFmpeg and Node.js are installed in the container.
- yt-dlp is configured to use Node.js as its JavaScript runtime.
- Playlists are disabled.
- Converted files are deleted after processing.
- A safety limit is applied before Telegram upload.
- Container instances are kept alive through the Container lifecycle hook and a five-minute scheduled health/start check.
- Cloudflare Container instances use ephemeral disk.

Only download media you own or are authorized to download, and comply with YouTube's terms and applicable copyright law.
