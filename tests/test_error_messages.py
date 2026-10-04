import asyncio
from pathlib import Path

import pytest

from app.claude import AgentSDKRunner, ClaudeCall, ClaudeError, claude_error
from app.errors import PodcastError, format_error
from app.models import Script
from app.pipeline.script import problems_in_german, validate
from app.tts import edge
from tests.conftest import make_script


def test_unknown_claude_failures_are_described():
    crash = claude_error("Claude failed: subtype=error_during_execution, stop=None, api_status=None, errors=[]")
    assert crash.message.startswith("Claude Code ist während der Arbeit abgestürzt.") and crash.transient
    exit_code = claude_error("ProcessError: Command failed with exit code 1")
    assert "Exit-Code 1" in exit_code.message
    assert "ohne Ergebnis" in claude_error("Claude Code ended without a result message").message


def test_usage_limit_names_the_reset_time(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    error = claude_error("Claude AI usage limit reached|1759590000")
    assert "Nutzungslimit" in error.message and "Wieder verfügbar ab" in error.message and "Uhr" in error.message
    assert not error.transient


async def test_failed_call_is_named_and_counts_attempts(monkeypatch, tmp_path):
    runner = AgentSDKRunner()
    monkeypatch.setattr(AgentSDKRunner, "RETRY_DELAYS", (0, 0))

    async def overloaded(call):
        raise claude_error("API Error: 529 Overloaded")

    monkeypatch.setattr(runner, "_run_once", overloaded)
    with pytest.raises(ClaudeError) as error:
        await runner.run(ClaudeCall(prompt="x", cwd=tmp_path, tools=[], label="Scout „Kritik“"))
    assert error.value.message.startswith("Scout „Kritik“: Die Claude-Server sind gerade überlastet")
    assert error.value.message.endswith("(Automatisch 3-mal versucht.)")
    assert error.value.cause.startswith("Die Claude-Server")
    assert error.value.details.count("Attempt") == 2

    calls = []

    async def counted(call):
        calls.append(1)
        raise claude_error("API Error: 529 Overloaded")

    monkeypatch.setattr(runner, "_run_once", counted)
    with pytest.raises(ClaudeError):  # quick checks do not wait for retries
        await runner.run(ClaudeCall(prompt="x", cwd=tmp_path, tools=[], retry=False))
    assert len(calls) == 1


def test_script_problems_are_summarized_in_german():
    data = make_script(words_per_line=4)
    data["chapters"][0]["lines"][0]["text"] = "Es gilt E = mc²."
    problems = validate(Script.model_validate(data), target_words=500)
    summary = problems_in_german(problems)
    assert "zu kurz (49 statt etwa 500 Wörter)" in summary and "Formeln" in summary


def test_unexpected_errors_are_german_and_name_the_step():
    try:
        {}["x"]
    except KeyError as exc:
        text = format_error(exc, "script")
    assert text.startswith("Unerwarteter Fehler im Schritt „Skript“ (KeyError: 'x')")
    assert "Traceback" in text


async def test_edge_failure_says_what_and_how_far(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(edge, "RETRY_DELAYS", (0,))

    class Communicate:
        def __init__(self, text, *args, **kwargs):
            self.text = text

        async def save(self, path):
            if "kaputt" in self.text:
                raise type("NoAudioReceived", (Exception,), {})("No audio was received.")
            Path(path).write_bytes(b"mp3")

    import edge_tts
    monkeypatch.setattr(edge_tts, "Communicate", Communicate)
    data = make_script(words_per_line=4)
    data["chapters"][2]["lines"][3]["text"] = "kaputt"
    tts = edge.EdgeTTS(settings.model_copy(update={"edge_concurrency": 1}))
    with pytest.raises(PodcastError) as error:
        await tts.synthesize(Script.model_validate(data), tmp_path)
    message = error.value.message
    assert "keinen Ton geliefert" in message and "11 von 12 Sätzen sind fertig" in message
    assert "Fortsetzen" in message and "Text: kaputt" in error.value.details
