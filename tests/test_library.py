import json
import shutil

from app import library
from app.db import JobStore
from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth, Script
from app.pipeline import create_job, load_context, run_pipeline
from app.pipeline.script import validate
from app.prompts import PromptStore
from tests.conftest import FakeClaude, FakeTTS, default_responses, make_script, mock_downloads
from tests.test_web import csrf, login, make_client, web_settings  # noqa: F401


def new_job(settings, topic="Festkörperbatterien", **opts):
    request = EpisodeRequest(topic=topic, options=EpisodeOptions(length=Length.kurz,
                                                                 research_depth=ResearchDepth.quick, **opts))
    return create_job(settings, request, PromptStore(settings.prompts_dir))


async def run(settings, job, claude):
    ctx = load_context(job, settings, claude=claude, tts=FakeTTS())
    ctx.download_options = mock_downloads()
    await run_pipeline(ctx)


async def test_episodes_remember_and_link_each_other(settings):
    first = new_job(settings)
    claude = FakeClaude(default_responses())
    await run(settings, first, claude)

    memory = json.loads((first / "memory.json").read_text())
    assert memory["mini_summary"].startswith("Solid-state") and len(memory["follow_ups"]) == 2
    assert memory["references"] == [] and memory["follows"] == ""
    (call,) = claude.calls_for("EpisodeMemory")
    assert "Wort Wort" in call.prompt and "Earlier episodes" not in call.prompt
    assert "Earlier episodes" not in claude.calls_for("Script")[0].prompt

    # The second episode continues the first and refers back to it.
    script = make_script(words_per_line=4)
    script["episode_references"] = [{"id": first.name, "how": "Knüpft an die Dendriten an."}]
    second = new_job(settings, topic="Dendriten", follows=first.name)
    claude = FakeClaude(default_responses(script))
    await run(settings, second, claude)

    (script_call,) = claude.calls_for("Script")
    assert f"`{first.name}`" in script_call.prompt and "Solid-state batteries" in script_call.prompt
    assert "This episode continues an earlier one" in script_call.prompt
    assert "relay race" in script_call.prompt  # the parent's long summary, in full
    assert (second / "library" / f"{first.name}.md").read_text().count("relay race") == 1
    plan_call = claude.calls_for("ResearchPlan")[0]
    assert "Solid-state batteries" in plan_call.prompt and "relay race" in plan_call.prompt
    assert "Solid-state batteries" in claude.calls_for("EpisodeMemory")[0].prompt

    memory = json.loads((second / "memory.json").read_text())
    assert memory["follows"] == first.name and memory["references"][0]["id"] == first.name
    links = library.links(settings.episodes_dir, first.name)
    assert links["incoming"] == [{"id": second.name, "title": "Testepisode", "how": "", "kind": "follows"}]
    assert library.links(settings.episodes_dir, second.name)["outgoing"][0]["id"] == first.name


async def test_only_mini_summaries_go_into_the_prompt(settings):
    first = new_job(settings)
    await run(settings, first, FakeClaude(default_responses()))
    second = new_job(settings, topic="Etwas anderes")
    claude = FakeClaude(default_responses())
    await run(settings, second, claude)
    prompt = claude.calls_for("Script")[0].prompt
    assert "Solid-state batteries" in prompt and "relay race" not in prompt
    assert "library/<ID>.md" in prompt


def test_unknown_episode_references_are_rejected():
    data = make_script()
    data["episode_references"] = [{"id": "20990101-000000-gibts-nicht", "how": "x"}]
    script = Script.model_validate(data)
    assert any("unknown episodes" in p for p in validate(script, script.word_count, known_episodes=set()))
    assert validate(script, script.word_count, known_episodes={"20990101-000000-gibts-nicht"}) == []


async def test_memory_failure_does_not_fail_the_episode(settings):
    responses = default_responses()
    responses["EpisodeMemory"] = [lambda call: (_ for _ in ()).throw(RuntimeError("weg"))]
    job = new_job(settings)
    await run(settings, job, FakeClaude(responses))
    assert (job / "episode.mp3").exists() and not (job / "memory.json").exists()
    assert "memory_failed" in (job / "log.jsonl").read_text()

    # Later, only the memory is made (no new speech even when the clips are gone).
    shutil.rmtree(job / "clips")
    tts = FakeTTS()
    ctx = load_context(job, settings, claude=FakeClaude(default_responses()), tts=tts)
    await run_pipeline(ctx, from_stage="memory")
    assert (job / "memory.json").exists() and tts.calls == 0


async def test_follow_up_from_the_website(web_settings):
    first = new_job(web_settings)
    await run(web_settings, first, FakeClaude(default_responses()))
    JobStore(web_settings.db_path).add(first.name, "Festkörperbatterien")
    JobStore(web_settings.db_path).update(first.name, status="done", title="Testepisode")

    with make_client(web_settings) as client:
        login(client)
        page = client.get(f"/episodes/{first.name}")
        assert "Wie geht's weiter?" in page.text and "Dendriten im Detail" in page.text
        assert f"follows={first.name}" in page.text

        form = client.get(f"/?topic=Dendriten&follows={first.name}")
        assert "Folgeepisode zu" in form.text and f'name="follows" value="{first.name}"' in form.text
        assert client.get("/?follows=../../etc").text.count('name="follows"') == 0

        response = client.post(f"/episodes/{first.name}/follow-ups/1", data={"csrf": csrf(client)})
        new_id = response.url.path.rsplit("/", 1)[1]
        request = json.loads((web_settings.episodes_dir / new_id / "request.json").read_text())["request"]
        assert request["topic"] == "Natrium-Ionen-Batterien als günstige Alternative"
        assert request["options"]["follows"] == first.name and request["options"]["length"] == "kurz"

        response = client.post(f"/episodes/{first.name}/follow-ups/7", data={"csrf": csrf(client)})
        assert "gibt es nicht" in response.text
