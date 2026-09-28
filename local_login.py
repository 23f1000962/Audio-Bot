#!/usr/bin/env python3
"""
Audio-Bot local YouTube login helper.

Purpose:
    Open YouTube in the user's own browser, let the user authenticate normally,
    then use yt-dlp's supported --cookies-from-browser mechanism to create a
    Netscape cookies.txt file that can be uploaded privately to the Telegram bot.

This script NEVER asks for or stores a Google password.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "youtube-cookies.txt"
URL = "https://www.youtube.com/robots.txt"

BROWSERS = {
    "1": ("chrome", "Google Chrome"),
    "2": ("brave", "Brave"),
    "3": ("edge", "Microsoft Edge"),
    "4": ("firefox", "Firefox"),
}


def check_ytdlp() -> bool:
    return shutil.which("yt-dlp") is not None


def main() -> int:
    print("=" * 56)
    print(" Audio Bot - YouTube Login Helper")
    print("=" * 56)
    print()
    print("This helper does NOT ask for your Google password.")
    print("It opens YouTube in your browser; you log in there normally.")
    print()

    if not check_ytdlp():
        print("ERROR: yt-dlp is not installed or is not in PATH.")
        print("Install it with:")
        print("  python -m pip install -U yt-dlp")
        return 1

    print("Choose the browser where you will log into YouTube:")
    for key, (_, label) in BROWSERS.items():
        print(f"  {key}. {label}")
    choice = input("\nBrowser [1-4]: ").strip()

    if choice not in BROWSERS:
        print("Invalid browser selection.")
        return 1

    browser, label = BROWSERS[choice]

    print()
    print(f"Opening YouTube in {label}...")
    webbrowser.open("https://www.youtube.com/")
    print()
    print("1. Log into your YouTube account in the browser.")
    print("2. Confirm that YouTube works normally in that browser.")
    print("3. Close all YouTube tabs before continuing, if possible.")
    print()
    input("Press ENTER here after you have finished logging in... ")

    # Give the browser a moment to release its cookie database.
    time.sleep(2)

    if OUTPUT.exists():
        OUTPUT.unlink()

    print()
    print("Extracting the YouTube browser cookies with yt-dlp...")
    print("No cookie contents will be printed.")

    command = [
        "yt-dlp",
        "--cookies-from-browser",
        browser,
        "--cookies",
        str(OUTPUT),
        "--skip-download",
        "--no-warnings",
        URL,
    ]

    try:
        result = subprocess.run(command, text=True, check=False)
    except OSError as exc:
        print(f"ERROR: Could not start yt-dlp: {exc}")
        return 1

    if result.returncode != 0 or not OUTPUT.exists():
        print()
        print("ERROR: yt-dlp could not export the browser cookies.")
        print()
        print("Common causes:")
        print("- The selected browser is not the one you logged into.")
        print("- The browser is still running and has its cookie database locked.")
        print("- The browser profile is protected or uses an unsupported setup.")
        print()
        print("Try closing the browser completely and run this helper again.")
        return 1

    size = OUTPUT.stat().st_size
    if size <= 0:
        print("ERROR: The exported cookie file is empty.")
        return 1

    print()
    print("SUCCESS")
    print(f"Cookie file: {OUTPUT}")
    print(f"Size: {size:,} bytes")
    print()
    print("Next step:")
    print("1. Open your private Audio Bot chat.")
    print("2. Send /login for the upload instructions.")
    print("3. Send youtube-cookies.txt as a Telegram DOCUMENT.")
    print("4. Delete this local file after the bot confirms activation.")
    print()
    print("WARNING: youtube-cookies.txt is an authenticated session credential.")
    print("Do not upload it to GitHub or share it with anyone else.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
