FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /data/downloads \
    && chown -R bot:bot /app /data

USER bot

ENV PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/data/downloads \
    STATE_FILE=/data/state/telegram.json

CMD ["python", "-m", "bot"]
