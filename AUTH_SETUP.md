# Audio Bot — Personal YouTube Login

## Important

This login flow does **not** ask for your Google password.

Current yt-dlp YouTube authentication uses a browser-exported cookie jar. The bot's `/login` command tells the owner how to export the YouTube cookies and then accepts the resulting `cookies.txt` as a Telegram **Document**.

## Render environment variable

Add:

```text
OWNER_TELEGRAM_ID=YOUR_TELEGRAM_USER_ID
```

The `/login`, `/auth`, `/logout`, and cookie-upload flow only works for that Telegram user ID.

Keep your existing:

```text
BOT_TOKEN=...
RENDER_EXTERNAL_URL=https://YOUR-BOT.onrender.com
YOUTUBE_COOKIE_FILE=/etc/secrets/cookies.txt
YOUTUBE_RUNTIME_COOKIE_FILE=/tmp/youtube-cookies.txt
```

`YOUTUBE_COOKIE_FILE` is optional if you intend to use `/login` after every restart. A valid Render Secret File named `cookies.txt` remains a useful fallback.

## First login

1. Deploy the updated files.
2. Set `OWNER_TELEGRAM_ID` in Render.
3. Open a new private/incognito browser window.
4. Log into your YouTube account.
5. In the same private session, open `https://www.youtube.com/robots.txt`.
6. Export the `youtube.com` cookies in Netscape/Mozilla `cookies.txt` format.
7. In your Telegram bot, send `/login`.
8. Send the exported `cookies.txt` **as a Document**.
9. Run `/auth`.

The bot validates the file before activating it and does not print cookie contents.

## Commands

```text
/login   Personal YouTube authentication instructions
/auth    Show authentication status
/logout  Remove the runtime /login cookie
```

## Render persistence note

Render Free services have an ephemeral filesystem. The runtime cookie uploaded through `/login` is stored under `/tmp`, so it can disappear after a restart/redeploy. A Render Secret File can be used as the persistent configuration source instead.

## Security

A YouTube cookie file is an authenticated session credential. Do not commit it to GitHub, paste it into chat, or share it with anyone. The bot attempts to delete the incoming Telegram document message after processing.
