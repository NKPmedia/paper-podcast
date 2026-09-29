"""Server entry point: web UI + background worker (later also Telegram bot and scheduler)."""

from __future__ import annotations

import logging

import uvicorn

from app.config import get_settings
from app.web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        proxy_headers=True,  # behind the reverse proxy: real client IP for login throttling
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
