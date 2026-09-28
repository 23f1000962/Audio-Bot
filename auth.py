"""YouTube.js TV OAuth authentication for Audio-Bot.

The OAuth2 flow is intentionally isolated from the normal WEB YouTube.js
client. Current YouTube.js documentation states that OAuth2 works with the
TV InnerTube client; normal WEB clients should use cookies instead.
"""
from __future__ import annotations

import os
import requests
from telegram import Update
from telegram.ext import ContextTypes

OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0") or "0")
ENGINE_URL = os.getenv(
    "YOUTUBE_ENGINE_URL",
    "http://127.0.0.1:8765",
).rstrip("/")

# Engine startup/OAuth initialization can take longer than a normal status
# request on a cold Render instance.
TIMEOUT = int(os.getenv("AUTH_ENGINE_TIMEOUT", "60"))


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(
        user
        and OWNER_TELEGRAM_ID > 0
        and user.id == OWNER_TELEGRAM_ID
    )


def _engine(method: str, path: str, payload=None):
    url = ENGINE_URL + path
    response = requests.request(
        method,
        url,
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def auth_status_text() -> str:
    try:
        data = _engine("GET", "/auth/status")
    except Exception as exc:
        return (
            "🔐 *YouTube Authentication*\n\n"
            "❌ Authentication service unavailable.\n\n"
            f"`{type(exc).__name__}: {exc}`"
        )

    status = data.get("status")

    if status == "active":
        return (
            "🔐 *YouTube Authentication*\n\n"
            "Status: ✅ Active\n"
            "Method: YouTube.js TV OAuth\n\n"
            "Your YouTube session is authenticated."
        )

    if status == "pending":
        return (
            "🔐 *YouTube Authentication*\n\n"
            "Status: ⏳ Waiting for authorization\n\n"
            f"Open: {data.get('verification_url', '')}\n"
            f"Code: `{data.get('user_code', '')}`"
        )

    if status == "error":
        return (
            "🔐 *YouTube Authentication*\n\n"
            "Status: ❌ Error\n\n"
            f"{data.get('message', 'Authentication failed.')}"
        )

    return (
        "🔐 *YouTube Authentication*\n\n"
        "Status: ❌ Not configured\n\n"
        "Send /login to start the Android/browser login."
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

    try:
        data = _engine("POST", "/auth/start")
    except requests.Timeout:
        await update.message.reply_text(
            "⏱️ The YouTube authentication service is still starting.\n\n"
            "Please wait a few seconds and send /login again."
        )
        return
    except Exception as exc:
        await update.message.reply_text(
            "❌ Authentication service is not ready.\n\n"
            f"`{type(exc).__name__}: {exc}`",
            parse_mode="Markdown",
        )
        return

    status = data.get("status")

    if status == "active":
        await update.message.reply_text(
            auth_status_text(),
            parse_mode="Markdown",
        )
        return

    if status == "pending":
        await update.message.reply_text(
            "📱 *YouTube Login*\n\n"
            "1. Tap the verification link.\n"
            "2. Sign in to Google/YouTube.\n"
            "3. Enter the displayed code if requested.\n"
            "4. Approve the device.\n"
            "5. Return here and send /auth.\n\n"
            f"🌐 {data.get('verification_url', '')}\n"
            f"🔑 Code: `{data.get('user_code', '')}`\n\n"
            "🔒 Your Google password is entered only on Google/YouTube. "
            "The Telegram bot never receives it.",
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )
        return

    if status == "starting":
        await update.message.reply_text(
            "⏳ YouTube authentication is starting.\n\n"
            "Please wait a few seconds, then send /auth."
        )
        return

    await update.message.reply_text(
        "⚠️ The YouTube login could not be started.\n\n"
        f"{data.get('message', 'Please try /login again.')}"
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

    try:
        _engine("POST", "/auth/logout")
        await update.message.reply_text(
            "🔓 *YouTube OAuth session removed.*\n\n"
            "Use /login whenever you want to authorize again.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Logout failed: {exc}"
        )


async def handle_cookie_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    # Kept for compatibility with the existing bot. This version uses OAuth.
    if update.message and update.message.document and is_owner(update):
        await update.message.reply_text(
            "ℹ️ This version uses YouTube.js TV OAuth.\n\n"
            "No cookies.txt upload is required. Use /login instead."
        )
