import base64
import json
import wave

import httpx
import pytest

from app.models import Script
from app.tts import make_tts
from app.tts.edge import EdgeTTS
from app.tts.gemini import FallbackTTS, GeminiTTS, QuotaExceeded, chunk_lines, speaker_label
from tests.conftest import FakeTTS, make_script


def test_speaker_labels_and_chunks():
    assert speaker_label("Dr. Jonas", "Expert") == "Jonas"
    assert speaker_label("Lena", "Host") == "Lena"
    assert speaker_label("Prof. Dr.", "Expert") == "Expert"
    lines = [("host", "a" * 2000), ("expert", "b" * 2000), ("host", "c" * 100)]
    assert [len(c) for c in chunk_lines(lines)] == [1, 2]


def test_provider_selection(settings):
    assert isinstance(make_tts(settings), EdgeTTS)
    with_key = settings.model_copy(update={"gemini_api_key": "k"})
    tts = make_tts(with_key)
    assert isinstance(tts, FallbackTTS) and isinstance(tts.primary, GeminiTTS)
    assert isinstance(make_tts(with_key.model_copy(update={"tts_provider": "edge"})), EdgeTTS)


def pcm_response(seconds=0.2, rate=24000):
    pcm = b"\x01\x00" * int(rate * seconds)
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": f"audio/L16;codec=pcm;rate={rate}", "data": base64.b64encode(pcm).decode()}}]}}]})


async def test_gemini_synthesizes_one_clip_per_chapter(settings, tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == "key"
        return pcm_response()

    settings = settings.model_copy(update={"gemini_api_key": "key"})
    tts = GeminiTTS(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    script = Script.model_validate(make_script(words_per_line=4))
    clips = await tts.synthesize(script, tmp_path)
    assert [c.chapter for c in clips] == [0, 1, 2] and len(requests) == 3
    body = requests[0]
    text = body["contents"][0]["parts"][0]["text"]
    assert "Lena: Wort Wort" in text and "Jonas: Wort" in text
    voices = body["generationConfig"]["speechConfig"]["multiSpeakerVoiceConfig"]["speakerVoiceConfigs"]
    assert {v["speaker"]: v["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] for v in voices} == {
        "Lena": "Kore", "Jonas": "Charon"}
    with wave.open(str(clips[0].path)) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 4800

    # resume: existing chunks are not requested again
    await tts.synthesize(script, tmp_path)
    assert len(requests) == 3


async def test_quota_falls_back_to_edge_for_whole_episode(settings, tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return pcm_response()

    settings = settings.model_copy(update={"gemini_api_key": "key"})
    gemini = GeminiTTS(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    fallback = FakeTTS()
    tts = FallbackTTS(gemini, fallback)
    script = Script.model_validate(make_script(words_per_line=4))
    clips = await tts.synthesize(script, tmp_path)
    assert tts.name == "fake" and "Kontingent" in tts.note
    assert fallback.calls == 1 and all(c.path.suffix == ".mp3" for c in clips)
    assert not list(tmp_path.glob("g_*.wav"))  # no mixed voices


async def test_gemini_retries_then_fails(settings, tmp_path, monkeypatch):
    async def no_sleep(seconds):
        return None

    monkeypatch.setattr("app.tts.gemini.asyncio.sleep", no_sleep)
    settings = settings.model_copy(update={"gemini_api_key": "key"})
    tts = GeminiTTS(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(500, text="boom"))))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await tts.synthesize(Script.model_validate(make_script()), tmp_path)
