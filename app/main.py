"""Server entry point: web UI, background worker and Telegram bot."""

from __future__ import annotations

import logging

import uvicorn

from app.claude import claude_auth_configured
from app.config import get_settings
from app.web import create_app

log = logging.getLogger("app")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.web_password_hash:
        log.error(
            "WEB_PASSWORD_HASH fehlt. Erzeugen mit:\n"
            "    docker compose run --rm app python -m app.cli hash-password\n"
            "und die ausgegebene Zeile in die .env eintragen, dann: docker compose up -d"
        )
        raise SystemExit(2)
    if not claude_auth_configured():
        log.warning("CLAUDE_CODE_OAUTH_TOKEN fehlt – Episoden werden fehlschlagen. Token mit `claude setup-token` erzeugen.")
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
