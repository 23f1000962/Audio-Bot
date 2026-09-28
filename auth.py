"""Android-first YouTube authentication for Audio-Bot.

Authentication uses YouTube.js' OAuth device flow. The bot never asks for a
Google password and never receives a browser cookie file.
"""
from __future__ import annotations

import os
import requests
from telegram import Update
from telegram.ext import ContextTypes

OWNER_TELEGRAM_ID = int(os.getenv('OWNER_TELEGRAM_ID', '0') or '0')
ENGINE_URL = os.getenv('YOUTUBE_ENGINE_URL', 'http://127.0.0.1:8765').rstrip('/')
TIMEOUT = 15


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and OWNER_TELEGRAM_ID > 0 and user.id == OWNER_TELEGRAM_ID)


def _engine(method: str, path: str, payload=None):
    url = ENGINE_URL + path
    r = requests.request(method, url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def auth_status_text() -> str:
    try:
        data = _engine('GET', '/auth/status')
    except Exception as exc:
        return f"🔐 *YouTube Authentication*\n\n❌ Authentication service unavailable.\n\n`{type(exc).__name__}`"

    status = data.get('status')
    if status == 'active':
        return (
            '🔐 *YouTube Authentication*\n\n'
            'Status: ✅ Active\n'
            'Method: YouTube.js OAuth device login\n'
            'Browser: 📱 Any Android browser\n\n'
            'The bot has an authenticated YouTube account session.'
        )
    if status == 'pending':
        return (
            '🔐 *YouTube Authentication*\n\n'
            'Status: ⏳ Waiting for authorization\n\n'
            f"Open: {data.get('verification_url', '')}\n"
            f"Code: `{data.get('user_code', '')}`"
        )
    if status == 'error':
        return f"🔐 *YouTube Authentication*\n\n❌ {data.get('message', 'Authentication failed.')}"
    return (
        '🔐 *YouTube Authentication*\n\n'
        'Status: ❌ Not configured\n\n'
        'Send /login to start the Android browser login.'
    )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_owner(update):
        await update.message.reply_text('⛔ This command is restricted to the bot owner.')
        return

    try:
        data = _engine('POST', '/auth/start')
    except Exception as exc:
        await update.message.reply_text(
            '❌ Authentication service is not ready.\n\n'
            f'`{type(exc).__name__}: {exc}`',
            parse_mode='Markdown',
        )
        return

    if data.get('status') == 'active':
        await update.message.reply_text(auth_status_text(), parse_mode='Markdown')
        return

    if data.get('status') == 'pending':
        await update.message.reply_text(
            '📱 *YouTube Login — Android*\n\n'
            '1. Tap the verification link below.\n'
            '2. Sign in to Google/YouTube normally.\n'
            '3. Enter the displayed code if YouTube asks for it.\n'
            '4. Approve the device.\n'
            '5. Return here and send /auth.\n\n'
            f"🌐 {data.get('verification_url')}\n"
            f"🔑 Code: `{data.get('user_code')}`\n\n"
            '🔒 Your Google password is entered only on Google/YouTube. '
            'The Telegram bot never receives it and no cookies.txt upload is required.',
            parse_mode='Markdown',
            disable_web_page_preview=False,
        )
        return

    await update.message.reply_text(
        '⚠️ The mobile login could not be started.\n\n'
        f"{data.get('message', 'Please try /login again.')}",
    )


async def auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_owner(update):
        await update.message.reply_text('⛔ This command is restricted to the bot owner.')
        return
    await update.message.reply_text(auth_status_text(), parse_mode='Markdown')


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_owner(update):
        await update.message.reply_text('⛔ This command is restricted to the bot owner.')
        return
    try:
        _engine('POST', '/auth/logout')
        await update.message.reply_text(
            '🔓 *YouTube OAuth session removed.*\n\n'
            'Use /login whenever you want to authorize the bot again.',
            parse_mode='Markdown',
        )
    except Exception as exc:
        await update.message.reply_text(f'❌ Logout failed: {exc}')


async def handle_cookie_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Kept as a compatibility handler. The Android OAuth version deliberately
    # does not accept cookies.txt credentials.
    if update.message and update.message.document and is_owner(update):
        await update.message.reply_text(
            'ℹ️ This version uses Android OAuth device login.\n\n'
            'No cookies.txt file is required. Use /login instead.'
        )
