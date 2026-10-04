import asyncio
import json

import pytest

from app.claude import AgentSDKRunner, ClaudeCall, ClaudeError, claude_error
from app.db import JobStore
from app.series import SeriesStore
from tests.conftest import FakeClaude, default_responses
from tests.test_library import new_job, run
from tests.test_web import csrf, login, make_client, web_settings  # noqa: F401


async def test_series_parts_recap_and_teaser(settings):
    store = SeriesStore(settings.series_path, settings.episodes_dir)
    first = new_job(settings)
    series = store.create("Batterien der Zukunft", "Von Festkörper bis Natrium", [first.name])
    claude = FakeClaude(default_responses())
    await run(settings, first, claude)
    prompt = claude.calls_for("Script")[0].prompt
    assert 'part 1 of the series "Batterien der Zukunft"' in prompt and "This is the first part" in prompt
    assert "Make the **first** suggestion the natural next part" in claude.calls_for("EpisodeMemory")[0].prompt

    second = new_job(settings, topic="Dendriten", follows=first.name)
    store.add_episode(series["id"], second.name)
    claude = FakeClaude(default_responses())
    await run(settings, second, claude)
    prompt = claude.calls_for("Script")[0].prompt
    assert "part 2 of the series" in prompt and "previously on" in prompt and "Von Festkörper bis Natrium" in prompt
    assert prompt.count("relay race") == 1  # previous part in full, not twice via `follows`
    assert "This episode continues an earlier one" not in prompt
    plan = claude.calls_for("ResearchPlan")[0].prompt
    assert "part 2 of the series" in plan and "relay race" in plan

    assert store.of_episode(second.name)[1] == 2
    store.move(series["id"], second.name, -1)
    assert store.get(series["id"])["episodes"] == [second.name, first.name]
    store.delete(series["id"])
    assert store.of_episode(first.name) is None and first.exists()


async def test_audience_changes_the_guidance(settings):
    await run(settings, new_job(settings), FakeClaude(default_responses()))
    claude = FakeClaude(default_responses())
    await run(settings, new_job(settings, topic="Mehr Batterien"), claude)
    regular = claude.calls_for("Script")[0].prompt
    assert "The listener has heard the earlier episodes" in regular and "concepts: solid electrolyte" in regular

    claude = FakeClaude(default_responses())
    await run(settings, new_job(settings, topic="Batterien für alle", audience="newcomer"), claude)
    newcomer = claude.calls_for("Script")[0].prompt
    assert "listeners who are new to the podcast" in newcomer and "has heard the earlier" not in newcomer


def test_web_series_flow(web_settings):
    first = new_job(web_settings)
    asyncio.run(run(web_settings, first, FakeClaude(default_responses())))
    jobs = JobStore(web_settings.db_path)
    jobs.add(first.name, "Festkörperbatterien")
    jobs.update(first.name, status="done", title="Testepisode")

    with make_client(web_settings) as client:
        login(client)
        page = client.get(f"/episodes/{first.name}")
        assert "Zu einer Reihe hinzufügen" in page.text
        client.post(f"/episodes/{first.name}/series",
                    data={"csrf": csrf(client), "series": "new", "title": "Akkus", "arc": "Roter Faden"})
        store = SeriesStore(web_settings.series_path, web_settings.episodes_dir)
        (series,) = store.all()
        assert series["episodes"] == [first.name]
        page = client.get(f"/episodes/{first.name}")
        assert "Reihe: Akkus · Teil 1 von 1" in page.text
        assert "Akkus, Teil 1" in client.get("/").text
        assert "Akkus" in client.get("/series").text

        # A follow-up of a series part continues the series and keeps the audience.
        response = client.post(f"/episodes/{first.name}/follow-ups/0", data={"csrf": csrf(client)})
        new_id = response.url.path.rsplit("/", 1)[1]
        assert store.get(series["id"])["episodes"] == [first.name, new_id]
        options = json.loads((web_settings.episodes_dir / new_id / "request.json").read_text())["request"]["options"]
        assert options["audience"] == "regular"

        # The form: choose a series and the audience.
        response = client.post("/episodes", data={
            "csrf": csrf(client), "topic": "Teil drei", "length": "kurz", "depth": "quick",
            "series": series["id"], "audience": "newcomer",
        })
        third = response.url.path.rsplit("/", 1)[1]
        assert store.of_episode(third)[1] == 3
        client.post(f"/series/{series['id']}", data={"csrf": csrf(client), "action": "delete"})
        assert store.all() == []


def test_transient_claude_errors():
    assert claude_error("Claude failed: api_status=429").transient
    assert claude_error("ProcessError: Command failed with exit code -9").transient
    assert "Arbeitsspeicher" in claude_error("ProcessError: Command failed with exit code -9").message
    assert claude_error("something odd").transient
    assert not claude_error("Claude AI usage limit reached|1759590000").transient
    assert not claude_error("invalid api key").transient


async def test_runner_retries_transient_failures(monkeypatch, tmp_path):
    runner = AgentSDKRunner()
    monkeypatch.setattr(AgentSDKRunner, "RETRY_DELAYS", (0, 0))
    attempts = []

    async def flaky(call):
        attempts.append(1)
        if len(attempts) < 3:
            raise claude_error("Claude failed: api_status=529 overloaded")
        return "ok"

    monkeypatch.setattr(runner, "_run_once", flaky)
    assert await runner.run(ClaudeCall(prompt="x", cwd=tmp_path, tools=[])) == "ok" and len(attempts) == 3

    async def broken(call):
        attempts.append(1)
        raise claude_error("invalid api key")

    attempts.clear()
    monkeypatch.setattr(runner, "_run_once", broken)
    with pytest.raises(ClaudeError):
        await runner.run(ClaudeCall(prompt="x", cwd=tmp_path, tools=[]))
    assert len(attempts) == 1


async def test_scout_failures_keep_their_details(settings):
    responses = default_responses()
    responses["ScoutResult"] = [lambda call: (_ for _ in ()).throw(
        ClaudeError("Claude ist mit einem Fehler abgebrochen.", "subtype=error_during_execution api_status=None"))
    ] + [default_responses()["ScoutResult"][0]] * 9
    job = new_job(settings)
    await run(settings, job, FakeClaude(responses))
    log = (job / "log.jsonl").read_text()
    assert "scout_failed" in log and "error_during_execution" in log
