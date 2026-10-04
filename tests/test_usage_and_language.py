from app.claude import token_usage
from app.episode_log import fmt_tokens, timeline, token_totals
from app.models import EpisodeOptions, EpisodeRequest, Language
from app.pipeline import prompt_context
from app.prompts import PromptStore, render_stage
from app.skills import SkillStore
from app.tts.edge import EdgeTTS


def test_token_usage_prefers_model_usage():
    model_usage = {
        "opus": {"inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 10},
        "haiku": {"inputTokens": 5, "outputTokens": 1, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
    }
    usage = {"input_tokens": 1, "output_tokens": 1}
    assert token_usage(model_usage, usage) == {"input": 105, "output": 51, "cache_read": 1000, "cache_write": 10}
    assert token_usage(None, {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2}) == {
        "input": 7, "output": 3, "cache_read": 2, "cache_write": 0}
    assert token_usage(None, None) == {}


def test_token_totals_and_timeline():
    entries = [
        {"ts": "2026-01-01T10:00:00+00:00", "event": "progress", "stage": "research", "message": "Claude recherchiert"},
        {"ts": "2026-01-01T10:00:00+00:00", "event": "stage_start", "stage": "research"},
        {"ts": "2026-01-01T10:01:00+00:00", "event": "claude", "stage": "research", "step": "scout:core",
         "model": "haiku", "turns": 4, "tokens": {"input": 10, "output": 5, "cache_read": 100, "cache_write": 0}},
        {"ts": "2026-01-01T10:02:00+00:00", "event": "claude", "stage": "script", "model": "opus", "turns": 2},
        {"ts": "2026-01-01T10:03:00+00:00", "event": "stage_done", "stage": "research", "seconds": 75},
    ]
    totals = token_totals(entries)
    assert totals["total"] == 115 and totals["input"] == 10 and totals["calls"] == 1
    rows = timeline(entries)
    assert [r["text"] for r in rows][0] == "Recherche gestartet"  # the repeated progress line is dropped
    assert "Scout core" in rows[1]["text"] and "115 Tokens" in rows[1]["text"]
    assert rows[-1]["text"] == "Recherche fertig nach 1 min 15 s"
    assert fmt_tokens(1_234_567) == "1,23 Mio." and fmt_tokens(950) == "950"


def test_english_episode_prompts(settings):
    request = EpisodeRequest(topic="x", options=EpisodeOptions(language=Language.en, handout=True))
    blocks = PromptStore(settings.prompts_dir).resolve(prompt_context(settings, request))
    assert "is in English" in blocks["system"]
    assert '"you"' in blocks["style"] and "first-name terms" in blocks["personas"]
    assert "You'll find the exact formula in the handout." in blocks["script_rules"]
    german = PromptStore(settings.prompts_dir).resolve(prompt_context(settings, EpisodeRequest(topic="x")))
    assert "is in German" in german["system"] and '"ihr"' in german["style"]


def test_handout_stage_names_sources_section_in_episode_language(settings):
    from app.models import Script

    script = Script(title="T", summary="S", chapters=[])
    common = dict(blocks={"handout": "h"}, script=script, sources=[], papers=[], notes="n")
    assert "\\section{Sources}" in render_stage("handout", language="en", language_name="English", **common)
    assert "\\section{Quellen}" in render_stage("handout", language="de", language_name="German", **common)


def test_renamed_skill_in_saved_config(settings):
    config = settings.data_dir / "skills.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"stages": {"script": ["german-podcast-dialogue", "fact-check"]}}')
    store = SkillStore(settings.skills_dir, config)
    assert store.stage_skills("script") == ["podcast-dialogue", "fact-check"]


def test_edge_voices_follow_language(settings):
    assert EdgeTTS(settings, "en").voices["host"] == settings.edge_voice_host_en
    assert EdgeTTS(settings).voices["expert"] == settings.edge_voice_expert


def test_feed_text_follows_the_languages(settings):
    from app.feed import episode_language, show_notes

    episode = {"summary": "S", "chapters": [{"start_ms": 0, "title": "Intro"}],
               "sources": [{"title": "Paper", "url": "https://x.org"}]}
    english = show_notes({**episode, "language": "en"}, None, "https://pod/e/1", "en")
    assert "<b>Chapters</b>" in english and "<b>Sources</b>" in english and "Open in the browser" in english
    assert "<b>Kapitel</b>" in show_notes(episode, None, None, "de")
    job_dir = settings.data_dir / "old-episode"
    job_dir.mkdir(parents=True)
    (job_dir / "request.json").write_text('{"request": {"options": {"language": "en"}}}')
    assert episode_language({}, job_dir) == "en"  # older episodes: from the request
    assert episode_language({"language": "de"}, job_dir) == "de"


def test_clarify_prompt_uses_the_episode_language():
    text = render_stage("clarify", topic="The Mamba paper", extra_instructions="", max_questions=10, searches=3,
                        language_name="German")
    assert "Write questions and premade answers in German" in text
