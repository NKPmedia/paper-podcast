"""Microsoft Edge online TTS (free, no API key) via the ``edge-tts`` package."""

from __future__ import annotations

import asyncio
import logging
import random
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

# Edge throttles long sessions with many requests for a while; wait it out (~4 min in total)
# instead of failing the whole episode on a short burst of errors.
RETRY_DELAYS = (5, 15, 45, 90, 120)


def failure_reason(exc: BaseException) -> str:
    """What went wrong with Edge, in words for the listener."""
    name = type(exc).__name__
    text = f"{name} {exc}".lower()
    if "403" in text or "handshake" in text or name == "SkewAdjustmentError":
        return ("Microsoft hat die Anfragen abgelehnt (HTTP 403). Meist drosselt Edge nach sehr vielen Sätzen "
                "kurzzeitig; selten ist die edge-tts-Version veraltet (dann hilft ein Update des Images)")
    if "429" in text or "too many" in text:
        return "Edge hat zu viele Anfragen kurz hintereinander abgelehnt (Drosselung)"
    if name == "NoAudioReceived" or "empty audio" in text:
        return ("Edge hat für einen Satz keinen Ton geliefert. Das passiert bei Drosselung oder bei Text, den die "
                "Stimme nicht sprechen kann (z.B. nur Sonderzeichen)")
    if name in ("ClientConnectorError", "ClientConnectionError", "ServerDisconnectedError", "TimeoutError",
                "ClientOSError", "gaierror") or "timeout" in text or "connect" in text:
        return "Der Server erreicht den Edge-Sprachdienst nicht (Verbindung abgebrochen, Zeitüberschreitung oder DNS)"
    return f"Edge TTS meldet einen unerwarteten Fehler ({name}: {str(exc)[:160]})"


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
        retries = len(RETRY_DELAYS) + 1
        for attempt in range(1, retries + 1):
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
                if attempt == retries:
                    raise PodcastError(
                        failure_reason(exc),
                        f"Edge TTS failed for {path.name} after {retries} attempts over ~{sum(RETRY_DELAYS) // 60} "
                        f"min: {type(exc).__name__}: {exc}\nText: {text[:300]}",
                    ) from exc
                delay = RETRY_DELAYS[attempt - 1] * random.uniform(0.8, 1.2)
                log.warning("Edge TTS %s failed (%s), retry in %.0fs", path.name, exc, delay)
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
        tasks = [asyncio.ensure_future(job) for job in jobs]
        try:
            await asyncio.gather(*tasks)
        except BaseException as exc:
            for task in tasks:  # stop the other requests; finished clips are kept for "Fortsetzen"
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not isinstance(exc, PodcastError):
                raise
            done = sum(1 for c in clips if c.path.exists())
            raise PodcastError(
                f"Die Sprachausgabe (Edge TTS) ist abgebrochen: {exc.message}. {done} von {len(clips)} Sätzen sind "
                f"fertig und bleiben gespeichert; ein Satz scheiterte auch nach {len(RETRY_DELAYS) + 1} Versuchen über "
                f"etwa {sum(RETRY_DELAYS) // 60} Minuten. In 10–15 Minuten mit „Fortsetzen“ weitermachen – es geht "
                "beim ersten fehlenden Satz weiter. Alternativ unter Einstellungen → Stimmen einen Gemini-Key "
                "eintragen.",
                exc.details,
            ) from exc
        return clips
