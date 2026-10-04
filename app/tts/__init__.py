"""Text-to-speech providers.

A provider turns a validated ``Script`` into audio clips. Each clip belongs to a
chapter; the audio stage stitches them together in order. Providers may produce one
clip per line (Edge) or per chapter chunk (multi-speaker engines such as Gemini).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.models import Script


@dataclass
class Clip:
    path: Path
    chapter: int
    speaker: str | None = None  # None for multi-speaker clips


class TTSProvider(Protocol):
    name: str

    async def synthesize(self, script: Script, out_dir: Path) -> list[Clip]: ...


TTS_LABELS = {"edge": "Edge", "gemini": "Gemini"}


def resolve_tts(settings: Settings, choice: str = "") -> str:
    """The engine an episode starts with: its own choice, else the settings default."""
    choice = choice or settings.tts_provider
    if choice == "auto":
        choice = "gemini" if settings.gemini_api_key else "edge"
    return choice if choice in TTS_LABELS else "edge"


def make_tts(settings: Settings, language: str = "de", choice: str = "") -> TTSProvider:
    """The chosen engine, with the other one as fallback when it fails.

    Edge falls back to Gemini (only with a Gemini key); Gemini falls back to Edge.
    Gemini chosen without a key starts with Edge right away.
    """
    from app.tts.edge import EdgeTTS

    edge = EdgeTTS(settings, language)
    if not settings.gemini_api_key:
        return edge
    from app.tts.gemini import FallbackTTS, GeminiTTS

    gemini = GeminiTTS(settings, language=language)
    if resolve_tts(settings, choice) == "gemini":
        return FallbackTTS(gemini, edge)
    return FallbackTTS(edge, gemini)
