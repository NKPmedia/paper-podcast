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
from app.models import Script
from app.tts import Clip
from app.tts.edge import clean_text

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_CHUNK_CHARS = 3500
RETRIES = 3


class QuotaExceeded(RuntimeError):
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

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client
        host = speaker_label(settings.host_name, "Host")
        expert = speaker_label(settings.expert_name, "Expert")
        if host == expert:
            host, expert = "Host", "Expert"
        self.labels = {"host": host, "expert": expert}

    def _body(self, chunk: list[tuple[str, str]]) -> dict:
        host, expert = self.labels["host"], self.labels["expert"]
        transcript = "\n".join(f"{self.labels[speaker]}: {text}" for speaker, text in chunk)
        prompt = (
            f"Lies das folgende Gespräch aus dem deutschen Wissenschafts-Podcast „{self.settings.podcast_name}“ "
            f"natürlich und im lockeren Gesprächston vor. {host} ist neugierig und warm, {expert} ruhig, "
            f"kompetent und begeistert.\n\n{transcript}"
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
                error = f"network error: {exc}"
            else:
                if response.status_code == 429 or "RESOURCE_EXHAUSTED" in response.text[:2000]:
                    raise QuotaExceeded(f"Gemini-Kontingent erschöpft ({response.status_code})")
                if response.status_code == 200:
                    part = response.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                    rate = int(re.search(r"rate=(\d+)", part.get("mimeType", "")).group(1)) \
                        if "rate=" in part.get("mimeType", "") else 24000
                    return base64.b64decode(part["data"]), rate
                error = f"HTTP {response.status_code}: {response.text[:300]}"
            if attempt == RETRIES:
                raise RuntimeError(f"Gemini TTS failed: {error}")
            log.warning("Gemini TTS attempt %d failed (%s), retrying", attempt, error)
            await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

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
    """Try the primary provider; on quota exhaustion (or any error) use the fallback for the whole episode."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.note = ""

    async def synthesize(self, script: Script, out_dir: Path) -> list[Clip]:
        try:
            clips = await self.primary.synthesize(script, out_dir)
            self.name = self.primary.name
            return clips
        except Exception as exc:
            log.warning("%s failed (%s); falling back to %s", self.primary.name, exc, self.fallback.name)
            self.note = f"{self.primary.name} → {self.fallback.name}: {exc}"
            for clip in out_dir.glob("*"):  # never mix voices of two engines in one episode
                clip.unlink()
            self.name = self.fallback.name
            return await self.fallback.synthesize(script, out_dir)
