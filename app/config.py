"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

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
    "API_TOKEN",
    "FEED_TOKEN",
    "GEMINI_API_KEY",
    "SMTP_PASSWORD",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
