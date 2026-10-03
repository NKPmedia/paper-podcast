"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent

# Environment variables that must never reach the Claude subprocess. Claude only
# needs its own token; everything else could leak through prompt injection.
SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "WEB_PASSWORD_HASH",
    "SESSION_SECRET",
    "FEED_TOKEN",
    "GEMINI_API_KEY",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")

    # Claude Code credentials (also settable in the setup dialog)
    claude_code_oauth_token: str = ""

    # Web UI
    web_password_hash: str = ""  # set in the setup dialog, or: python -m app.cli hash-password
    session_secret: str = ""  # empty = generated once and stored in the data dir
    cookie_secure: bool = True  # set false only for plain-http testing on a LAN
    port: int = 8000
    timezone: str = "Europe/Berlin"
    public_base_url: str = ""  # e.g. https://podcast.example.org, used for links in messages

    # Telegram bot (optional)
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""  # comma-separated; empty = setup mode (bot replies with your ID)
    telegram_notify_all: bool = True  # also announce and send episodes started from the web UI
    # Podcast feed token (empty = generated once and stored in the data dir)
    feed_token: str = ""

    # Optional self-hosted Bot API server (lifts the 50 MB upload limit), e.g. http://telegram-bot-api:8081
    telegram_api_base_url: str = ""

    @property
    def telegram_chat_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_chat_ids.replace(" ", "").split(",") if x}

    # Claude models per role: alias (haiku, sonnet, opus) or a full model ID
    research_scout_model: str = "haiku"
    research_main_model: str = "opus"
    script_model: str = "opus"
    claude_max_turns_script: int = 40

    # Paper downloads (full texts for the main research agent)
    download_max_mb: int = 20
    download_timeout_s: int = 60
    paper_max_chars: int = 250_000

    # Podcast defaults
    podcast_name: str = "Paper Podcast"
    host_name: str = "Lena"
    expert_name: str = "Dr. Jonas"
    words_per_minute: int = 140

    # Speech: auto = Gemini if GEMINI_API_KEY is set (Edge as fallback), else Edge
    tts_provider: str = "auto"  # auto | edge | gemini
    gemini_api_key: str = ""
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"
    gemini_voice_host: str = "Kore"
    gemini_voice_expert: str = "Charon"

    # Edge TTS
    edge_voice_host: str = "de-DE-SeraphinaMultilingualNeural"
    edge_voice_expert: str = "de-DE-FlorianMultilingualNeural"
    edge_concurrency: int = 3
    edge_proxy: str | None = None  # e.g. http://proxy:3128

    # Audio
    pause_line_ms: int = 350
    pause_chapter_ms: int = 1200
    target_lufs: float = -16.0
    mp3_bitrate: str = "96k"

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
        return self.data_dir / "jobs.sqlite3"


# Settings that can be changed in the web UI (first-run setup and settings page).
# They are stored in <data>/settings.json; values from the environment or .env win.
UI_SETTINGS = (
    "web_password_hash", "claude_code_oauth_token", "podcast_name", "host_name", "expert_name",
    "public_base_url", "cookie_secure", "telegram_bot_token", "telegram_allowed_chat_ids", "gemini_api_key",
)


def overrides_path(settings: Settings) -> Path:
    return settings.data_dir / "settings.json"


def locked(settings: Settings, key: str) -> bool:
    """True if the value comes from the environment / .env and cannot be changed in the UI."""
    return key in getattr(settings, "_env_fields", settings.model_fields_set)


def export_env(settings: Settings) -> None:
    """Claude Code reads its token from the environment of its subprocess."""
    if settings.claude_code_oauth_token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token


def apply_overrides(settings: Settings) -> Settings:
    object.__setattr__(settings, "_env_fields", set(settings.model_fields_set))
    path = overrides_path(settings)
    if path.exists():
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            if key in UI_SETTINGS and not locked(settings, key):
                setattr(settings, key, value)
    export_env(settings)
    return settings


def save_overrides(settings: Settings, values: dict) -> None:
    """Persist UI changes (owner-only file) and apply them to the running settings."""
    values = {k: v for k, v in values.items() if k in UI_SETTINGS and not locked(settings, k)}
    path = overrides_path(settings)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    for key, value in values.items():
        setattr(settings, key, value)
    export_env(settings)


@lru_cache
def get_settings() -> Settings:
    return apply_overrides(Settings())
