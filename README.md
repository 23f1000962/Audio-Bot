# Audio Bot — RapidAPI version

This update replaces the direct `yt-dlp` YouTube extraction flow with the RapidAPI service used in the RapidAPI screenshots.

## Required environment variables

- `BOT_TOKEN`
- `RAPIDAPI_KEY`

## RapidAPI endpoints

- `GET /api/v1/download`
- `GET /api/v1/progress?id=PROGRESS_ID`

The bot requests audio as:

- `format=mp3`
- `audioQuality=128`
- `addInfo=false`
- `allowExtendedDuration=false`

## GitHub update

Replace these files in the existing repository:

1. `bot.py`
2. `requirements.txt`
3. `Dockerfile`
4. `.env.example` (optional but recommended)

Then add `RAPIDAPI_KEY` to your hosting provider's environment variables.

Do not commit real API keys to GitHub.
