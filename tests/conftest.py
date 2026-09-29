from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.claude import ClaudeCall, ClaudeResult
from app.config import Settings
from app.models import Script
from app.tts import Clip


def make_script(words_per_line: int = 12, chapters: int = 3, lines: int = 4) -> dict:
    return {
        "title": "Testepisode",
        "summary": "Eine kurze Testepisode.",
        "chapters": [
            {
                "title": f"Kapitel {c + 1}",
                "lines": [
                    {"speaker": "host" if i % 2 == 0 else "expert", "text": " ".join(["Wort"] * words_per_line)}
                    for i in range(lines)
                ],
            }
            for c in range(chapters)
        ],
    }


RESEARCH = {
    "title_suggestion": "Testthema",
    "notes": "## Kernaussagen\n- Ergebnis A [Muster 2024]",
    "sources": [{"title": "Ein Paper", "authors": "Muster", "year": "2024", "url": "https://example.org"}],
}


class FakeClaude:
    """Returns queued structured outputs and records calls."""

    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.calls: list[ClaudeCall] = []

    async def run(self, call: ClaudeCall) -> ClaudeResult:
        self.calls.append(call)
        return ClaudeResult(
            structured=self.outputs.pop(0),
            text="",
            session_id=f"session-{len(self.calls)}",
            cost_usd=0.01,
            num_turns=3,
            skills_used=list(call.skills[:1]),
        )


class FakeTTS:
    """Writes short sine tones (different pitch per speaker) with ffmpeg."""

    name = "fake"

    def __init__(self):
        self.calls = 0

    async def synthesize(self, script: Script, out_dir: Path) -> list[Clip]:
        self.calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        for ci, li, line in script.iter_lines():
            path = out_dir / f"c{ci:02d}_l{li:03d}.mp3"
            freq = 440 if line.speaker == "host" else 220
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"sine=frequency={freq}:duration=0.4", "-c:a", "libmp3lame", str(path)],
                check=True,
            )
            clips.append(Clip(path=path, chapter=ci, speaker=line.speaker))
        return clips


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data", words_per_minute=10)
