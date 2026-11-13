# AI Video Cutter Bot — Telegram long-polling worker
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    XDG_CACHE_HOME=/app/.cache \
    YTDLP_COOKIES_FILE=/app/cookies.txt

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot/ ./bot/
# Netscape cookies for yt-dlp (must exist next to Dockerfile at build time)
COPY cookies.txt ./cookies.txt

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin bot \
    && mkdir -p /app/.data /app/.cache/videos /app/.cache/huggingface \
    && chown -R bot:bot /app

USER bot

# Persist SQLite + video cache + Whisper/HF models via volumes
VOLUME ["/app/.data", "/app/.cache"]

CMD ["python", "-m", "bot.main"]
