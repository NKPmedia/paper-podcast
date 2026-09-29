import pytest

from app.models import EpisodeRequest, EpisodeOptions, Length
from app.pipeline import prompt_context
from app.prompts import PromptStore, render_stage
from app.skills import SkillStore


def test_blocks_render_with_context(settings):
    store = PromptStore(settings.prompts_dir)
    ctx = prompt_context(settings, EpisodeRequest(topic="x", options=EpisodeOptions(length=Length.kurz)))
    blocks = store.resolve(ctx)
    assert set(blocks) == {"system", "personas", "style", "structure", "research", "script_rules", "handout"}
    assert "Lena" in blocks["personas"]
    assert "50 Wörter" in blocks["structure"]  # 5 min * 10 wpm in the test settings
    assert "mittel" in blocks["research"]


def test_override_reset_and_per_request_addition(settings):
    store = PromptStore(settings.prompts_dir)
    store.save("style", "Sehr förmlich, {{ host_name }} siezt.")
    assert store.is_overridden("style")
    blocks = store.resolve({"host_name": "Lena", **_ctx(settings)}, {"style": "Mehr Humor."})
    assert blocks["style"].startswith("Sehr förmlich, Lena siezt.")
    assert blocks["style"].endswith("Mehr Humor.")
    store.reset("style")
    assert not store.is_overridden("style")
    with pytest.raises(KeyError):
        store.resolve(_ctx(settings), {"nope": "x"})


def _ctx(settings):
    return prompt_context(settings, EpisodeRequest(topic="x"))


def test_stage_templates_render(settings):
    blocks = PromptStore(settings.prompts_dir).resolve(_ctx(settings))
    text = render_stage(
        "scout", blocks=blocks, topic="Quantencomputer", extra_instructions="", angle="Kritik", count=10, searches=6
    )
    assert "Quantencomputer" in text and "paper-research" in text and "Kritik" in text


def test_skills_discovery_override_and_install(settings, tmp_path):
    store = SkillStore(settings.skills_dir, settings.data_dir / "skills.json")
    skills = store.all()
    assert {"paper-research", "german-podcast-dialogue", "tts-friendly-text", "fact-check", "handout-plots"} <= set(skills)
    assert skills["paper-research"].description

    own = settings.skills_dir / "mein-skill"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: mein-skill\ndescription: Test\n---\nInhalt\n")
    names = store.stage_skills("research", ["mein-skill"])
    assert names == ["paper-research", "mein-skill"]

    job = tmp_path / "job"
    store.install(job, names)
    assert (job / ".claude/skills/paper-research/reference/apis.md").exists()
    assert (job / ".claude/skills/mein-skill/SKILL.md").exists()

    with pytest.raises(KeyError):
        store.stage_skills("research", ["gibt-es-nicht"])


def test_stage_skill_config_file(settings):
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "skills.json").write_text('{"stages": {"script": ["fact-check"]}}')
    store = SkillStore(settings.skills_dir, settings.data_dir / "skills.json")
    assert store.stage_skills("script") == ["fact-check"]
    assert store.stage_skills("research") == ["paper-research"]


def test_script_rules_depend_on_handout(settings):
    store = PromptStore(settings.prompts_dir)
    without = store.resolve(prompt_context(settings, EpisodeRequest(topic="x")))["script_rules"]
    with_handout = store.resolve(
        prompt_context(settings, EpisodeRequest(topic="x", options=EpisodeOptions(handout=True)))
    )["script_rules"]
    assert "Keine Formeln vorlesen" in without and "Keine Formeln vorlesen" in with_handout
    assert "Es gibt kein Handout" in without and "handout_items" not in without
    assert "handout_items" in with_handout
