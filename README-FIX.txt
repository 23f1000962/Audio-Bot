Audio-Bot authentication fix

Replace these files in the repository:
- auth.py
- youtube_engine.mjs
- start.sh

Main changes:
- Uses YouTube.js TV client for OAuth.
- Uses session.signIn() and session auth events.
- Separates lightweight OAuth initialization from the normal downloader client.
- Avoids the old 15-second authentication timeout.
- Waits longer for the Node engine to become ready at startup.
- Preserves /login, /auth and /logout.
