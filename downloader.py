# downloader.py — single required change

Find:

"youtubepot-bgutilhttp": {
    "base_url":
        "http://127.0.0.1:4416",
},

Replace with:

"youtubepot-bgutilhttp": {
    "base_url":
        "https://audiobot-bgutil.onrender.com",
},

No other downloader.py changes are required for the remote BGUTIL setup.
Keep the existing cookies.txt handling unchanged.
