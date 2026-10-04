"""Google Gemini multi-speaker TTS (free tier works with an AI Studio key).

Gemini speaks a whole dialogue with two voices in one request, which sounds more
natural than stitching single lines. Each chapter (or a part of a long chapter) is
one request and becomes one WAV clip. Quota errors raise ``QuotaExceeded`` so the
caller can fall back to Edge TTS.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import wave
from pathlib import Path

import httpx

from app.config import Settings
from app.errors import PodcastError
from app.models import Script
from app.tts import Clip
from app.tts.edge import clean_text

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_CHUNK_CHARS = 3500
RETRIES = 3


def gemini_reason(status: int | None, error: str) -> str:
    text = error.lower()
    if status is None:
        return "Der Server erreicht die Gemini-API nicht (Netzwerkproblem). Prüfe die Internetverbindung des Servers."
    if status in (400, 401, 403) and ("api key" in text or "api_key" in text or "permission" in text or status != 400):
        return "Der Gemini-Key ist ungültig oder hat keinen Zugriff. Prüfe ihn unter Einstellungen → Stimmen."
    if status == 404 or "model" in text and status == 400:
        return "Das eingestellte Gemini-TTS-Modell gibt es nicht. Prüfe den Modellnamen unter Einstellungen → Stimmen."
    if status >= 500:
        return "Die Gemini-Server sind gerade gestört. Später mit „Fortsetzen“ erneut versuchen."
    return f"Gemini meldet HTTP {status}. Mit „Fortsetzen“ erneut versuchen; Einzelheiten unter „Technische Details“."


class QuotaExceeded(PodcastError):
    pass


def speaker_label(name: str, fallback: str) -> str:
    """Gemini speaker names must match the transcript; keep one plain word ('Dr. Jonas' -> 'Jonas')."""
    words = [w for w in name.split() if not w.endswith(".")]  # drop titles like "Dr." or "Prof."
    words = [re.sub(r"[^\wÄÖÜäöüß]", "", w) for w in words]
    words = [w for w in words if w]
    return words[-1] if words else fallback


def chunk_lines(lines: list[tuple[str, str]], limit: int = MAX_CHUNK_CHARS) -> list[list[tuple[str, str]]]:
    chunks, current, size = [], [], 0
    for speaker, text in lines:
        if current and size + len(text) > limit:
            chunks.append(current)
            current, size = [], 0
        current.append((speaker, text))
        size += len(text) + len(speaker) + 2
    if current:
        chunks.append(current)
    return chunks


class GeminiTTS:
    name = "gemini"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, language: str = "de"):
        self.settings = settings
        self.client = client
        self.language = language
        host = speaker_label(settings.host_name, "Host")
        expert = speaker_label(settings.expert_name, "Expert")
        if host == expert:
            host, expert = "Host", "Expert"
        self.labels = {"host": host, "expert": expert}

    def _body(self, chunk: list[tuple[str, str]]) -> dict:
        host, expert = self.labels["host"], self.labels["expert"]
        transcript = "\n".join(f"{self.labels[speaker]}: {text}" for speaker, text in chunk)
        language = "English" if self.language == "en" else "German"
        prompt = (
            f"Read the following conversation from the {language}-language science podcast "
            f"\"{self.settings.podcast_name}\" aloud in {language}, naturally and in a relaxed conversational tone. "
            f"{host} is curious and warm, {expert} calm, knowledgeable and enthusiastic.\n\n{transcript}"
        )
        voices = {host: self.settings.gemini_voice_host, expert: self.settings.gemini_voice_expert}
        return {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                    {"speaker": label, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
                    for label, voice in voices.items()
                ]}},
            },
        }

    async def _request(self, client: httpx.AsyncClient, body: dict) -> tuple[bytes, int]:
        url = API.format(model=self.settings.gemini_tts_model)
        for attempt in range(1, RETRIES + 1):
            try:
                response = await client.post(url, json=body, headers={"x-goog-api-key": self.settings.gemini_api_key})
            except httpx.HTTPError as exc:
                error = f"network error: {type(exc).__name__}: {exc}"
                status = None
            else:
                if response.status_code == 429 or "RESOURCE_EXHAUSTED" in response.text[:2000]:
                    raise QuotaExceeded(
                        "Das Gemini-Kontingent ist erschöpft (das kostenlose Kontingent reicht für wenige Episoden "
                        "pro Tag). Morgen mit „Fortsetzen“ weitermachen oder unter Einstellungen → Stimmen auf Edge "
                        "umstellen.", f"HTTP {response.status_code}: {response.text[:300]}")
                if response.status_code == 200:
                    part = response.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                    rate = int(re.search(r"rate=(\d+)", part.get("mimeType", "")).group(1)) \
                        if "rate=" in part.get("mimeType", "") else 24000
                    return base64.b64decode(part["data"]), rate
                error = f"HTTP {response.status_code}: {response.text[:300]}"
                status = response.status_code
                if status in (400, 401, 403, 404):
                    break  # a wrong key or model does not get better by retrying
            if attempt == RETRIES:
                break
            log.warning("Gemini TTS attempt %d failed (%s), retrying", attempt, error)
            await asyncio.sleep(2**attempt)
        raise PodcastError(f"Die Sprachausgabe mit Gemini ist fehlgeschlagen: {gemini_reason(status, error)}",
                           f"Gemini TTS failed ({self.settings.gemini_tts_model}): {error}")


    async def synthesize(self, script: Script, out_dir: Path) -> list[Clip]:
        out_dir.mkdir(parents=True, exist_ok=True)
        client = self.client or httpx.AsyncClient(timeout=300)
        clips: list[Clip] = []
        try:
            for ci, chapter in enumerate(script.chapters):
                lines = [(line.speaker, clean_text(line.text)) for line in chapter.lines if clean_text(line.text)]
                for pi, chunk in enumerate(chunk_lines(lines)):
                    path = out_dir / f"g_c{ci:02d}_p{pi:02d}.wav"
                    if not path.exists():  # finished chunks are kept when a job resumes
                        pcm, rate = await self._request(client, self._body(chunk))
                        tmp = path.with_suffix(".part")
                        with wave.open(str(tmp), "wb") as w:
                            w.setnchannels(1)
                            w.setsampwidth(2)
                            w.setframerate(rate)
                            w.writeframes(pcm)
                        tmp.rename(path)
                    clips.append(Clip(path=path, chapter=ci))
        finally:
            if client is not self.client:
                await client.aclose()
        return clips


class FallbackTTS:
    """Speak with the primary engine; if it fails, the other engine speaks the whole episode.

    Voices of two engines are never mixed: switching deletes the clips of the failed
    engine. ``engine.txt`` in the clip folder remembers a switch, so "Fortsetzen"
    goes on with the engine that took over (keeping its finished clips) and tries
    the original one only if that fails too.
    """

    MARKER = "engine.txt"

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.note = ""

    async def synthesize(self, script: Script, out_dir: Path) -> list[Clip]:
        out_dir.mkdir(parents=True, exist_ok=True)
        marker = out_dir / self.MARKER
        engines = [self.primary, self.fallback]
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == self.fallback.name:
            engines.reverse()  # resumed after a switch: keep going with the engine that took over
            self.note = f"Fortgesetzt mit {self.fallback.name} (Ersatz für {self.primary.name})"
        errors: list[tuple[str, Exception]] = []
        for engine in engines:
            if errors:  # switching engines: drop the other engine's clips
                for clip in out_dir.glob("*"):
                    if clip.name != self.MARKER:
                        clip.unlink()
                marker.write_text(engine.name, encoding="utf-8")
            try:
                clips = await engine.synthesize(script, out_dir)
            except Exception as exc:
                log.warning("%s failed (%s)", engine.name, exc)
                errors.append((engine.name, exc))
                continue
            self.name = engine.name
            if errors:
                failed, exc = errors[-1]
                self.note = f"{failed} → {engine.name}: {getattr(exc, 'message', exc)}"
            return clips
        (first, first_exc), (second, second_exc) = errors
        raise PodcastError(
            f"Beide Sprachausgaben sind gescheitert. {first}: {getattr(first_exc, 'message', first_exc)} – "
            f"Ersatz {second}: {getattr(second_exc, 'message', second_exc)}",
            "\n\n".join(f"{name}: {type(exc).__name__}: {exc}\n{getattr(exc, 'details', '')}"
                         for name, exc in errors),
        ) from second_exc
