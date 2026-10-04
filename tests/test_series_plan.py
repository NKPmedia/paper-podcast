import asyncio
import json

from app.db import JobStore
from app.jobs import JobService, Worker
from app.models import EpisodeOptions, EpisodeRequest, Length
from app.pipeline import load_context
from app.prompts import PromptStore
from app.series import SeriesStore
from tests.conftest import SERIES_PLAN, FakeClaude, FakeTTS, default_responses, mock_downloads
from tests.test_web import csrf, login, make_client, web_settings  # noqa: F401

OPTIONS = {"length": "kurz", "language": "de", "audience": "regular", "handout": False, "tts": "",
           "research_depth": "quick"}


def setup(settings, responses=None):
    store = JobStore(settings.db_path)
    service = JobService(settings, store, PromptStore(settings.prompts_dir))
    claude = FakeClaude(responses or default_responses())

    def factory(job_dir):
        ctx = load_context(job_dir, settings, claude=claude, tts=FakeTTS())
        ctx.download_options = mock_downloads()
        return ctx

    worker = Worker(service, factory)
    return service, worker, claude, SeriesStore(settings.series_path, settings.episodes_dir)


async def run_all(service, worker):
    while (job := service.store.claim_next()) is not None:
        await worker.run_job(job)


async def test_planning_researches_deeply_and_stores_the_plan(settings):
    service, worker, claude, series_store = setup(settings)
    series = series_store.create("Batterien")
    job = service.plan_series(series["id"], "Festkörperbatterien verstehen", 3, OPTIONS)
    assert job.kind == "series_plan" and service.store.list() == []  # not in the episode list
    await run_all(service, worker)

    done = service.store.get(job.id)
    assert done.status == "done" and done.title == "Batterien von morgen"
    plan_call = claude.calls_for("ResearchPlan")[0]
    assert "whole podcast series" in plan_call.prompt and "between 3 and 6 scout tasks" in plan_call.prompt
    (call,) = claude.calls_for("SeriesPlanResult")
    assert "Plan exactly **3** new parts" in call.prompt and "Festkörperbatterien verstehen" in call.prompt
    assert call.label == "Reihenplanung" and "WebSearch" in call.tools

    stored = series_store.get(series["id"])
    assert stored["title"] == "Batterien von morgen" and stored["arc"].startswith("Von den Grundlagen")
    items = stored["plan"]["items"]
    assert [i["title"] for i in items] == ["Wie Ionen wandern", "Das Dendriten-Problem", "Natrium statt Lithium"]
    assert items[1]["builds_on"] == [1]  # part 7 does not exist: dropped


async def test_planned_parts_know_their_role_and_revision_keeps_them(settings):
    responses = default_responses()
    for key in ("Selection", "ResearchResult", "Script", "EpisodeMemory"):
        responses[key] = responses[key] * 3  # the planning research plus one episode
    service, worker, claude, series_store = setup(settings, responses)
    series = series_store.create("Batterien")
    plan_job = service.plan_series(series["id"], "Festkörperbatterien", 3, OPTIONS)
    await run_all(service, worker)

    # The first planned part becomes an episode of the series.
    item = series_store.get(series["id"])["plan"]["items"][0]
    job = service.submit(EpisodeRequest(topic=item["topic"], options=EpisodeOptions(length=Length.kurz)))
    series_store.assign_item(series["id"], 0, job.id)
    await run_all(service, worker)
    script = claude.calls_for("Script")[-1].prompt
    assert "This part's role in the series plan. Goal: Verstehen, warum feste Elektrolyte" in script
    assert "Das Dendriten-Problem" in script and 'next planned part, "Das Dendriten-Problem"' in script
    assert series_store.get(series["id"])["episodes"] == [job.id]

    # Revise with the listener's own words: only the plan is made again, the research is reused.
    revised = {**SERIES_PLAN, "changes": "Natrium gestrichen.", "episodes": SERIES_PLAN["episodes"][1:2]}
    claude.responses["SeriesPlanResult"] = [revised]
    scouts_before = len(claude.calls_for("ScoutResult"))
    service.revise_series_plan(plan_job.id, "Bitte ohne Natrium")
    await run_all(service, worker)
    call = claude.calls_for("SeriesPlanResult")[-1]
    assert "Bitte ohne Natrium" in call.prompt and "These parts already exist" in call.prompt
    assert "1. Testepisode" in call.prompt and len(claude.calls_for("ScoutResult")) == scouts_before
    plan = series_store.get(series["id"])["plan"]
    assert [i["title"] for i in plan["items"]] == ["Das Dendriten-Problem"]
    assert plan["changes"] == "Natrium gestrichen." and plan["history"][0]["instruction"] == "Bitte ohne Natrium"
    assert job.id in plan["assigned"]


async def test_wrong_number_of_parts_is_corrected(settings):
    responses = default_responses()
    responses["SeriesPlanResult"] = [{**SERIES_PLAN, "episodes": SERIES_PLAN["episodes"][:2]}, SERIES_PLAN]
    service, worker, claude, series_store = setup(settings, responses)
    series = series_store.create("Batterien")
    service.plan_series(series["id"], "Festkörperbatterien", 3, OPTIONS)
    await run_all(service, worker)
    calls = claude.calls_for("SeriesPlanResult")
    assert len(calls) == 2 and "Plan exactly 3 new parts (you returned 2)" in calls[1].prompt


def test_series_planning_on_the_website(web_settings):
    with make_client(web_settings) as client:
        login(client)
        assert "Reihe von Claude planen lassen" in client.get("/series").text
        client.post("/series/plan", data={"csrf": csrf(client), "goal": "Festkörperbatterien verstehen",
                                          "count": "3", "length": "kurz", "language": "de", "depth": "quick"})
        app = client.app
        series_store = SeriesStore(web_settings.series_path, web_settings.episodes_dir)
        (series,) = series_store.all()
        job = app.state.service.store.claim_next()
        assert job.kind == "series_plan" and series["plan"]["job"] == job.id
        assert "Planung:" in client.get("/series").text  # status while it runs
        asyncio.run(app.state.worker.run_job(job))

        page = client.get("/series").text
        assert "Batterien von morgen" in page and "Wie Ionen wandern" in page and "Planung anpassen" in page
        assert "Festkörperbatterien verstehen" not in client.get("/").text  # plan jobs are not episodes
        assert "Reihenplanung" in client.get(f"/episodes/{job.id}").text

        client.post(f"/series/{series['id']}/plan/items", data={"csrf": csrf(client), "action": "all"})
        stored = series_store.get(series["id"])
        assert len(stored["episodes"]) == 3 and stored["plan"]["items"] == []
        requests = [json.loads((web_settings.episodes_dir / i / "request.json").read_text())["request"]
                    for i in stored["episodes"]]
        assert [r["topic"] for r in requests][0] == "Grundlagen fester Elektrolyte (Muster 2024)"
        assert requests[0]["options"]["length"] == "kurz" and requests[0]["options"]["research_depth"] == "quick"

        client.post(f"/series/{series['id']}/plan", data={"csrf": csrf(client), "action": "revise",
                                                           "instruction": "Mehr Robotik"})
        assert app.state.service.store.get(job.id).status == "queued"
