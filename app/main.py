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
    if settings.web_password_hash and not claude_auth_configured():
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
