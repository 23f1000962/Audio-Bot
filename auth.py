"""
Personal YouTube authentication helper for Audio-Bot.

This does NOT collect a Google username/password.

yt-dlp currently uses a browser-exported YouTube cookie jar for this kind of
server-side authentication. The Telegram bot provides a private /login flow:

    /login
      -> export a fresh YouTube cookies.txt in a private/incognito browser
      -> send cookies.txt to this private bot as a DOCUMENT
      -> bot validates it and stores a runtime copy

The cookie file is treated as a credential and is never printed to logs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes


OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0") or "0")

COOKIE_FILE = Path(
    os.getenv("YOUTUBE_COOKIE_FILE", "/etc/secrets/cookies.txt")
)

RUNTIME_COOKIE_FILE = Path(
    os.getenv("YOUTUBE_RUNTIME_COOKIE_FILE", "/tmp/youtube-cookies.txt")
)

MAX_COOKIE_FILE_BYTES = 1024 * 1024


def is_owner(update: Update) -> bool:
    """Return True only for the configured personal Telegram account."""
    user = update.effective_user

    if not user or OWNER_TELEGRAM_ID <= 0:
        return False

    return user.id == OWNER_TELEGRAM_ID


def _is_valid_cookie_file(path: Path) -> bool:
    """Validate a Mozilla/Netscape-format cookie file."""
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False

        if path.stat().st_size > MAX_COOKIE_FILE_BYTES:
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

            if len(line.split("\t")) != 7:
                return False

        return True

    except (OSError, UnicodeError):
        return False


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def runtime_cookie_valid() -> bool:
    return _is_valid_cookie_file(RUNTIME_COOKIE_FILE)


def secret_cookie_valid() -> bool:
    return _is_valid_cookie_file(COOKIE_FILE)


def auth_status_text() -> str:
    runtime_ok = runtime_cookie_valid()
    secret_ok = secret_cookie_valid()

    if runtime_ok:
        return (
            "🔐 *YouTube Authentication*\n\n"
            "Status: ✅ Active\n"
            "Source: Personal `/login` session\n"
            "Cookie jar: ✅ Valid\n\n"
            "The bot is ready to use the authenticated YouTube session."
        )

    if secret_ok:
        return (
            "🔐 *YouTube Authentication*\n\n"
            "Status: ✅ Active\n"
            "Source: Render Secret File\n"
            "Cookie jar: ✅ Valid\n\n"
            "The bot has a server-side YouTube cookie jar available."
        )

    if COOKIE_FILE.exists():
        return (
            "🔐 *YouTube Authentication*\n\n"
            "Status: ❌ Invalid\n"
            "The configured cookies.txt is not a valid Netscape/Mozilla "
            "cookie export."
        )

    return (
        "🔐 *YouTube Authentication*\n\n"
        "Status: ❌ Not configured\n\n"
        "Use /login to authenticate your personal YouTube session."
    )


async def login_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    if not is_owner(update):
        await update.message.reply_text(
            "⛔ This command is restricted to the bot owner."
        )
        return

    await update.message.reply_text(
        "🔐 *Personal YouTube Login*\n\n"
        "This bot does not ask for your Google password.\n\n"
        "*1.* Open a new private/incognito browser window.\n"
        "*2.* Log in to your YouTube account there.\n"
        "*3.* In that same private window/tab open:\n"
        "`https://www.youtube.com/robots.txt`\n"
        "*4.* Export the `youtube.com` cookies as a Netscape/Mozilla "
        "`cookies.txt` file.\n"
        "*5.* Send that file here as a *Document*, not as pasted text.\n\n"
        "⚠️ `cookies.txt` is effectively an authenticated session. "
        "Do not send it to anyone else or commit it to GitHub.\n\n"
        "After upload, the bot will validate it and activate it for "
        "yt-dlp.\n\n"
        "Use /auth to check the status or /logout to remove the runtime "
        "session.",
        parse_mode="Markdown",
    )


async def auth_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    if not is_owner(update):
        await update.message.reply_text(
            "⛔ This command is restricted to the bot owner."
        )
        return

    await update.message.reply_text(
        auth_status_text(),
        parse_mode="Markdown",
    )


async def logout_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    if not is_owner(update):
        await update.message.reply_text(
            "⛔ This command is restricted to the bot owner."
        )
        return

    removed = False

    try:
        if RUNTIME_COOKIE_FILE.exists():
            RUNTIME_COOKIE_FILE.unlink()
            removed = True
    except OSError:
        pass

    if removed:
        message = (
            "🔓 *Runtime YouTube session removed.*\n\n"
            "The /login-uploaded cookie has been deleted from the runtime."
        )

        if secret_cookie_valid():
            message += (
                "\n\n⚠️ A separate Render Secret File cookies.txt is still "
                "configured, so downloads may continue using that server-side "
                "session."
            )
    else:
        message = "ℹ️ No runtime /login session was present."

    await update.message.reply_text(
        message,
        parse_mode="Markdown",
    )


async def handle_cookie_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Accept and validate a private user's exported cookies.txt."""
    if not update.message or not update.message.document:
        return

    if not is_owner(update):
        await update.message.reply_text(
            "⛔ Cookie login is restricted to the bot owner."
        )
        return

    document = update.message.document
    filename = (document.file_name or "").lower()

    if not filename.endswith((".txt", ".cookies")):
        return

    if document.file_size and document.file_size > MAX_COOKIE_FILE_BYTES:
        await update.message.reply_text(
            "❌ Cookie file is larger than 1 MB."
        )
        return

    temp_path: Path | None = None

    try:
        fd, raw_path = tempfile.mkstemp(
            prefix="audio-bot-cookie-",
            suffix=".txt",
            dir="/tmp",
        )
        os.close(fd)
        temp_path = Path(raw_path)
        _chmod_private(temp_path)

        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(temp_path))

        if not _is_valid_cookie_file(temp_path):
            await update.message.reply_text(
                "❌ Invalid cookie file.\n\n"
                "Expected a Netscape/Mozilla cookies.txt export. "
                "Do not upload .env contents or pasted environment variables."
            )
            return

        RUNTIME_COOKIE_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        new_path = RUNTIME_COOKIE_FILE.with_suffix(
            RUNTIME_COOKIE_FILE.suffix + ".new"
        )

        shutil.copyfile(temp_path, new_path)
        _chmod_private(new_path)

        if not _is_valid_cookie_file(new_path):
            raise RuntimeError("The runtime cookie copy failed validation.")

        os.replace(new_path, RUNTIME_COOKIE_FILE)
        _chmod_private(RUNTIME_COOKIE_FILE)

        await update.message.reply_text(
            "✅ *YouTube login activated.*\n\n"
            "Your cookie jar passed validation and is now available to "
            "yt-dlp.\n\n"
            "Use /auth to verify the status.\n"
            "Use /logout to remove this runtime session.",
            parse_mode="Markdown",
        )

    except Exception as exc:
        print(
            "[Auth] Cookie upload failed:",
            type(exc).__name__,
        )
        await update.message.reply_text(
            "❌ Could not activate the cookie file. Please export a fresh "
            "Netscape-format YouTube cookies.txt and try again."
        )

    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

        # Try to remove the incoming Telegram document message so the
        # sensitive file is not left visible in the chat history.
        try:
            await update.message.delete()
        except Exception:
            pass
