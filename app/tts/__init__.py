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


def make_tts(settings: Settings) -> TTSProvider:
    from app.tts.edge import EdgeTTS

    return EdgeTTS(settings)
