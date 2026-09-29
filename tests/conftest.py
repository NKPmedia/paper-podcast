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
    "notes": "## Kernaussagen\n- Ergebnis A [Muster 2024, papers/arxiv_2401.00001.md:5-9]",
    "podcast_material": "- Analogie: wie ein Staffellauf [Muster 2024, papers/arxiv_2401.00001.md:12-14]",
    "sources": [{"title": "Ein Paper", "authors": "Muster", "year": "2024", "url": "https://example.org"}],
}


SCOUT = {
    "candidates": [
        {"title": "Ein Paper", "authors": "Muster", "year": "2024", "arxiv_id": "2401.00001v2",
         "score": 9, "reason": "Kernarbeit", "quote": "We show A."},
        {"title": "Kritik am Paper", "doi": "10.1000/xyz", "url": "https://example.org/kritik",
         "score": 5, "reason": "Kritik"},
    ]
}
SELECTION = {"focus": "Ergebnis A", "selected": [{"id": "arxiv:2401.00001", "reason": "Kern"}]}


def default_responses(script: dict | None = None) -> dict:
    return {
        "ScoutResult": [SCOUT] * 10,
        "Selection": [SELECTION],
        "ResearchResult": [RESEARCH],
        "Script": [script or make_script(words_per_line=4)],
    }


class FakeClaude:
    """Answers by requested output schema (scouts run in parallel, so order varies).

    ``responses`` maps a schema title to a list of outputs consumed in order; an
    output may be a callable taking the ``ClaudeCall``.
    """

    def __init__(self, responses: dict[str, list]):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[ClaudeCall] = []

    def calls_for(self, schema: str) -> list[ClaudeCall]:
        return [c for c in self.calls if c.output_schema and c.output_schema["schema"]["title"] == schema]

    async def run(self, call: ClaudeCall) -> ClaudeResult:
        self.calls.append(call)
        output = self.responses[call.output_schema["schema"]["title"]].pop(0)
        if callable(output):
            output = output(call)
        return ClaudeResult(
            structured=output,
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


ARXIV_HTML = """<html><head><title>x</title><script>var a=1;</script></head><body>
<nav>Navigation</nav>
<article class="ltx_document">
<h1 class="ltx_title">Ein Paper</h1>
<section><h2>1 Introduction</h2>
<p>We propose a method whose cost scales as <math alttext="O(n^{2})"><mi>O</mi></math> in the length.</p>
""" + "\n".join(f"<p>Paragraph {i}: " + "Result A holds across all settings. " * 12 + "</p>" for i in range(8)) + """
<table><tr><th>Model</th><th>BLEU</th></tr><tr><td>Ours</td><td>28.4</td></tr></table>
</section>
<section class="ltx_bibliography"><h2>References</h2><p>Very long reference list</p></section>
</article></body></html>"""


def mock_downloads(routes: dict | None = None) -> dict:
    """download_options for the pipeline context: an httpx client with canned responses."""
    import httpx

    routes = routes if routes is not None else {
        "https://arxiv.org/html/2401.00001": (200, "text/html; charset=utf-8", ARXIV_HTML.encode()),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        status, ctype, body = routes.get(str(request.url), (404, "text/plain", b"not found"))
        headers = {"content-type": ctype}
        if status in (301, 302):
            headers["location"] = body.decode()
            body = b""
        return httpx.Response(status, headers=headers, content=body)

    return {"client": httpx.AsyncClient(transport=httpx.MockTransport(handler)), "check_urls": False}
