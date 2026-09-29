FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CLAUDE_CONFIG_DIR=/data/claude

RUN mkdir -p /data && chown app:app /data
USER app
VOLUME ["/data"]

# Milestone 1: CLI only. The web server / Telegram bot will become the default command.
ENTRYPOINT ["python", "-m", "app.cli"]
CMD ["--help"]
