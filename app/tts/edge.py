"""Microsoft Edge online TTS (free, no API key) via the ``edge-tts`` package."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from app.config import Settings
from app.errors import PodcastError
from app.models import Script
from app.tts import Clip

log = logging.getLogger(__name__)

# Small prosody tweaks for the optional ``style`` hint of a line.
STYLE_PROSODY = {
    "begeistert": ("+8%", "+2Hz"),
    "aufgeregt": ("+8%", "+2Hz"),
    "überrascht": ("+5%", "+3Hz"),
    "neugierig": ("+3%", "+1Hz"),
    "nachdenklich": ("-6%", "-1Hz"),
    "ernst": ("-4%", "-2Hz"),
    "ruhig": ("-5%", "+0Hz"),
    "excited": ("+8%", "+2Hz"),
    "enthusiastic": ("+8%", "+2Hz"),
    "surprised": ("+5%", "+3Hz"),
    "curious": ("+3%", "+1Hz"),
    "thoughtful": ("-6%", "-1Hz"),
    "serious": ("-4%", "-2Hz"),
    "calm": ("-5%", "+0Hz"),
}

RETRIES = 4


def clean_text(text: str) -> str:
    """Strip markup that a TTS engine would read aloud or stumble over."""
    text = re.sub(r"[*_#`>\[\]{}]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def prosody(style: str) -> tuple[str, str]:
    style = style.lower()
    for key, value in STYLE_PROSODY.items():
        if key in style:
            return value
    return "+0%", "+0Hz"


class EdgeTTS:
    name = "edge"

    def __init__(self, settings: Settings, language: str = "de"):
        self.settings = settings
        if language == "en":
            self.voices = {"host": settings.edge_voice_host_en, "expert": settings.edge_voice_expert_en}
        else:
            self.voices = {"host": settings.edge_voice_host, "expert": settings.edge_voice_expert}

    async def _synthesize_line(self, text: str, voice: str, style: str, path: Path) -> None:
        import edge_tts

        rate, pitch = prosody(style)
        tmp = path.with_suffix(".part")
        for attempt in range(1, RETRIES + 1):
            try:
                communicate = edge_tts.Communicate(
                    text, voice, rate=rate, pitch=pitch, proxy=self.settings.edge_proxy
                )
                await communicate.save(str(tmp))
                if tmp.stat().st_size == 0:
                    raise RuntimeError("empty audio")
                tmp.rename(path)
                return
            except Exception as exc:  # network hiccups, throttling
                if attempt == RETRIES:
                    raise PodcastError(
                        "Die Sprachausgabe (Edge TTS) ist nicht erreichbar. Prüfe die Internetverbindung des Servers "
                        "oder trag unter Einstellungen → Stimmen einen Gemini-Key ein. Mit „Fortsetzen“ geht es "
                        "beim letzten Satz weiter.",
                        f"Edge TTS failed for {path.name} after {RETRIES} attempts: {type(exc).__name__}: {exc}",
                    ) from exc
                delay = 2**attempt
                log.warning("Edge TTS %s failed (%s), retry in %ss", path.name, exc, delay)
                await asyncio.sleep(delay)

    async def synthesize(self, script: Script, out_dir: Path) -> list[Clip]:
        out_dir.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(self.settings.edge_concurrency)
        clips: list[Clip] = []
        jobs = []

        async def one(text: str, voice: str, style: str, path: Path):
            async with semaphore:
                await self._synthesize_line(text, voice, style, path)

        for ci, li, line in script.iter_lines():
            path = out_dir / f"c{ci:02d}_l{li:03d}_{line.speaker}.mp3"
            clips.append(Clip(path=path, chapter=ci, speaker=line.speaker))
            text = clean_text(line.text)
            if not path.exists() and text:  # existing clips are kept on resume
                jobs.append(one(text, self.voices[line.speaker], line.style, path))
            elif not text:
                clips.pop()
        await asyncio.gather(*jobs)
        return clips
