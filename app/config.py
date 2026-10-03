"""Configuration, stored in the local SQLite database (``<data>/app.sqlite3``).

Everything is edited in the web UI: the essentials in the first-run setup dialog,
the rest on the settings page. There is no .env file. Only the data directory itself
comes from the environment (``DATA_DIR``, default ``/data``), because the database
lives inside it.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

APP_DIR = Path(__file__).resolve().parent
DB_NAME = "app.sqlite3"

# Environment variables that must never reach the Claude subprocess, should anyone
# set them anyway. Claude itself only needs its token.
SECRET_ENV_VARS = ("TELEGRAM_BOT_TOKEN", "WEB_PASSWORD_HASH", "SESSION_SECRET", "FEED_TOKEN", "GEMINI_API_KEY")


class Settings(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))

    # Claude Code credentials
    claude_code_oauth_token: str = ""

    # Web UI
    web_password_hash: str = ""  # set in the setup dialog
    session_secret: str = ""  # generated on first start
    cookie_secure: bool = True  # false only for plain-http testing on a LAN
    port: int = 8000
    timezone: str = "Europe/Berlin"
    public_base_url: str = ""  # e.g. https://podcast.example.org, used for links in messages

    # Telegram bot (optional)
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""  # comma-separated; empty = setup mode (bot replies with your ID)
    telegram_notify_all: bool = True  # also announce and send episodes started from the web UI
    telegram_api_base_url: str = ""  # optional self-hosted Bot API server (lifts the 50 MB limit)

    # Podcast feed
    feed_token: str = ""  # generated on first start

    # Claude models per role: alias (haiku, sonnet, opus) or a full model ID
    research_scout_model: str = "haiku"
    research_main_model: str = "opus"
    script_model: str = "opus"
    claude_max_turns_script: int = 40

    # Paper downloads (full texts for the main research agent)
    download_max_mb: int = 20
    download_timeout_s: int = 60
    # ~150k characters ≈ 35k tokens: the main body of almost any paper fits, while
    # several papers together still fit Claude's context when they are read.
    paper_max_chars: int = 150_000

    # Podcast
    podcast_name: str = "Paper Podcast"
    host_name: str = "Lena"
    expert_name: str = "Dr. Jonas"
    words_per_minute: int = 140
    default_language: str = "de"  # de | en: preselected language for new episodes

    # Speech: auto = Gemini if a Gemini key is set (Edge as fallback), else Edge
    tts_provider: str = "auto"  # auto | edge | gemini
    gemini_api_key: str = ""
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"
    gemini_voice_host: str = "Kore"
    gemini_voice_expert: str = "Charon"
    edge_voice_host: str = "de-DE-SeraphinaMultilingualNeural"
    edge_voice_expert: str = "de-DE-FlorianMultilingualNeural"
    edge_voice_host_en: str = "en-US-AvaMultilingualNeural"
    edge_voice_expert_en: str = "en-US-AndrewMultilingualNeural"
    edge_concurrency: int = 3
    edge_proxy: str | None = None

    # Audio
    pause_line_ms: int = 350
    pause_chapter_ms: int = 1200
    target_lufs: float = -16.0
    mp3_bitrate: str = "96k"

    @property
    def telegram_chat_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_chat_ids.replace(" ", "").split(",") if x}

    @property
    def episodes_dir(self) -> Path:
        return self.data_dir / "episodes"

    @property
    def prompts_dir(self) -> Path:
        return self.data_dir / "prompts"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME


STORED = tuple(name for name in Settings.model_fields if name != "data_dir")
GENERATED_SECRETS = ("session_secret", "feed_token")


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    new = not db_path.exists()
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    if new:
        db_path.chmod(0o600)  # holds the password hash and tokens
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return conn


def _export_env(settings: Settings) -> None:
    """Claude Code reads its token from the environment of its subprocess."""
    if settings.claude_code_oauth_token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token


def _migrate_legacy(settings: Settings) -> None:
    """Earlier versions used jobs.sqlite3, settings.json and secret files; take them over once."""
    data = settings.data_dir
    old_db = data / "jobs.sqlite3"
    if old_db.exists() and not settings.db_path.exists():
        old_db.rename(settings.db_path)
    legacy = {}
    old_json = data / "settings.json"
    if old_json.exists():
        legacy.update({k: v for k, v in json.loads(old_json.read_text(encoding="utf-8")).items() if k in STORED})
    for key in GENERATED_SECRETS:
        path = data / key
        if path.exists():
            legacy.setdefault(key, path.read_text().strip())
    if legacy:
        save_settings(settings, legacy)
        for path in (old_json, data / "session_secret", data / "feed_token"):
            if path.exists():
                path.rename(path.with_name(path.name + ".migrated"))


def load_settings(data_dir: Path | None = None, **defaults) -> Settings:
    """Defaults, overlaid with the values stored in SQLite. Generates missing secrets."""
    settings = Settings(**({"data_dir": data_dir} if data_dir else {}), **defaults)
    _migrate_legacy(settings)
    with closing(_connect(settings.db_path)) as conn:
        stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    for key, raw in stored.items():
        if key in STORED:
            try:
                setattr(settings, key, json.loads(raw))
            except ValidationError:
                pass  # keep the default for a value that no longer validates
    ensure_secrets(settings)
    _export_env(settings)
    return settings


def ensure_secrets(settings: Settings) -> None:
    """Generate the session secret and feed token once and store them."""
    missing = {key: secrets.token_urlsafe(32 if key == "feed_token" else 48)
               for key in GENERATED_SECRETS if not getattr(settings, key)}
    if missing:
        save_settings(settings, missing)


def save_settings(settings: Settings, values: dict) -> None:
    """Validate, store and apply values. Raises ``SettingsError`` with messages per field."""
    unknown = [k for k in values if k not in STORED]
    if unknown:
        raise SettingsError({k: "Unbekannte Einstellung" for k in unknown})
    try:
        checked = Settings.model_validate({**settings.model_dump(), **values})
    except ValidationError as exc:
        raise SettingsError({str(e["loc"][0]): "Ungültiger Wert" for e in exc.errors()}) from None
    clean = {k: getattr(checked, k) for k in values}
    with closing(_connect(settings.db_path)) as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in clean.items()],
        )
    for key, value in clean.items():
        setattr(settings, key, value)
    _export_env(settings)


@lru_cache
def get_settings() -> Settings:
    return load_settings()
