# Python runtime for the Telegram Audio Bot
FROM python:3.13-slim

# Install FFmpeg, required by yt-dlp for MP3 audio extraction
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better build caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application
COPY . .

# Run the Telegram bot
CMD ["python", "bot.py"]
