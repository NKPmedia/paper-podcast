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


def make_tts(settings: Settings, language: str = "de") -> TTSProvider:
    """``auto``: Gemini (with Edge as fallback) when GEMINI_API_KEY is set, else Edge."""
    from app.tts.edge import EdgeTTS

    edge = EdgeTTS(settings, language)
    use_gemini = settings.tts_provider == "gemini" or (settings.tts_provider == "auto" and settings.gemini_api_key)
    if use_gemini and settings.gemini_api_key:
        from app.tts.gemini import FallbackTTS, GeminiTTS

        return FallbackTTS(GeminiTTS(settings, language=language), edge)
    return edge
