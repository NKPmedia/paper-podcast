import json
import subprocess

import pytest

from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth, Script
from app.pipeline import create_job, load_context, run_pipeline
from app.pipeline.script import validate
from app.prompts import PromptStore
from tests.conftest import FakeClaude, FakeTTS, default_responses, make_script, mock_downloads


def new_job(settings, **opts):
    request = EpisodeRequest(topic="Festkörperbatterien", options=EpisodeOptions(length=Length.kurz, **opts))
    return create_job(settings, request, PromptStore(settings.prompts_dir))


def context(settings, job, claude, tts=None):
    ctx = load_context(job, settings, claude=claude, tts=tts or FakeTTS())
    ctx.download_options = mock_downloads()
    return ctx


def test_validate_script():
    script = Script.model_validate(make_script())
    assert validate(script, target_words=script.word_count) == []
    problems = validate(script, target_words=script.word_count * 3)
    assert any("too short" in p for p in problems)
    one_speaker = make_script()
    for ch in one_speaker["chapters"]:
        for line in ch["lines"]:
            line["speaker"] = "host"
    assert any("Both speakers" in p for p in validate(Script.model_validate(one_speaker), 144))


async def test_full_pipeline(settings):
    # target: 5 min * 10 wpm = 50 words; script: 3 chapters * 4 lines * 4 words = 48
    claude = FakeClaude(default_responses())
    tts = FakeTTS()
    job = new_job(settings, research_depth=ResearchDepth.quick, block_overrides={"style": "Mit Humor."})
    ctx = context(settings, job, claude, tts)

    stages = []
    mp3 = await run_pipeline(ctx, progress=lambda s, d: stages.append(s))

    assert [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s] == ["research", "script", "tts", "audio"]
    assert mp3.exists() and mp3.stat().st_size > 1000
    research_md = (job / "research.md").read_text()
    assert research_md.startswith("# Testthema")
    assert "## Podcast material\n\n- Analogie: wie ein Staffellauf" in research_md
    assert json.loads((job / "sources.json").read_text())[0]["title"] == "Ein Paper"
    assert (job / "papers" / "arxiv_2401.00001.md").exists()

    scout = claude.calls_for("ScoutResult")[0]  # quick research → up to two scouts
    assert scout.model == "haiku" and scout.tools == ["WebSearch", "WebFetch", "Read"]
    assert scout.skills == ["paper-research"]
    assert "Festkörperbatterien" in scout.prompt
    (read,) = claude.calls_for("ResearchResult")
    assert read.model == "opus" and "papers/arxiv_2401.00001.md" in read.prompt
    assert "Keep the references" in read.prompt and "podcast_material" in read.prompt
    (script_call,) = claude.calls_for("Script")
    assert script_call.model == "opus"
    assert script_call.skills == ["podcast-dialogue", "tts-friendly-text", "fact-check"]
    assert "Ergebnis A" in script_call.prompt and "Mit Humor." in script_call.prompt
    assert script_call.tools == ["Read"]
    assert "# Full texts for reference" in script_call.prompt
    assert "`papers/arxiv_2401.00001.md`: Ein Paper (" in script_call.prompt
    assert "Staffellauf" in script_call.prompt
    assert (job / ".claude/skills/fact-check/SKILL.md").exists()

    episode = json.loads((job / "episode.json").read_text())
    assert [c["title"] for c in episode["chapters"]] == ["Kapitel 1", "Kapitel 2", "Kapitel 3"]
    # 12 clips * 0.4 s + pauses
    assert 6 <= episode["duration_seconds"] <= 10

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(mp3)],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout)
    assert len(info["chapters"]) == 3
    assert info["format"]["tags"]["title"] == "Testepisode"

    # Re-running skips all finished stages.
    stages.clear()
    await run_pipeline(ctx, progress=lambda s, d: stages.append(s))
    assert stages == [] and tts.calls == 1


async def test_script_retry_with_feedback(settings):
    responses = default_responses()
    responses["Script"] = [make_script(words_per_line=1), make_script(words_per_line=4)]
    claude = FakeClaude(responses)
    ctx = context(settings, new_job(settings), claude)
    await run_pipeline(ctx)
    first, retry = claude.calls_for("Script")
    assert retry.resume == f"session-{claude.calls.index(first) + 1}"
    assert "too short" in retry.prompt


async def test_from_stage_reruns_later_stages(settings):
    responses = default_responses()
    responses["Script"] = [make_script(words_per_line=4)] * 2
    claude = FakeClaude(responses)
    tts = FakeTTS()
    job = new_job(settings)
    ctx = context(settings, job, claude, tts)
    await run_pipeline(ctx)
    stale = job / "clips" / "stale.mp3"
    stale.write_bytes(b"x")
    await run_pipeline(ctx, from_stage="script")
    assert len(claude.calls_for("Script")) == 2 and len(claude.calls_for("ScoutResult")) == 3
    assert tts.calls == 2 and not stale.exists()


async def test_unknown_stage(settings):
    ctx = context(settings, new_job(settings), FakeClaude({}))
    with pytest.raises(ValueError):
        await run_pipeline(ctx, from_stage="nope")


def _with_line(text: str, **extra) -> Script:
    data = make_script(words_per_line=4)
    data["chapters"][0]["lines"][1]["text"] = text
    data.update(extra)
    return Script.model_validate(data)


@pytest.mark.parametrize("text", [
    "Die Attention ist softmax(QK^T / sqrt(d)) V.",
    "Es gilt E = mc².",
    "Man summiert x_i über alle i.",
    "Das ist \\frac{a}{b} im Prinzip.",
])
def test_written_formulas_are_rejected(text):
    problems = validate(_with_line(text), target_words=50)
    assert any("formula" in p for p in problems)


def test_spoken_explanation_is_fine():
    script = _with_line("Doppelt so langer Text heißt viermal so viel Rechenarbeit.")
    assert validate(script, target_words=script.word_count) == []


def test_handout_references_need_a_handout():
    script = _with_line("Die genaue Formel findet ihr im Handout.", handout_items=["Attention-Formel"])
    problems = validate(script, target_words=script.word_count, handout=False)
    assert any("refers to a handout" in p for p in problems)
    assert any("handout_items" in p for p in problems)
    assert validate(script, target_words=script.word_count, handout=True) == []
