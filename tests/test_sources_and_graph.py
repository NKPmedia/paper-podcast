import json
from types import SimpleNamespace

import pytest

from app.errors import PodcastError
from app.models import Candidate, EpisodeOptions, EpisodeRequest, Length, ResearchDepth, SourceFilter
from app.pipeline import create_job, load_context, run_pipeline
from app.pipeline.research import merge_candidates, passes_filter
from app.prompts import PromptStore
from app.web import series_graph
from tests.conftest import FakeClaude, FakeTTS, default_responses, mock_downloads

PREPRINT = {"title": "Ein Paper", "arxiv_id": "2401.00001", "score": 9, "reason": "Kern",
            "venue": "", "peer_reviewed": "no"}
JOURNAL = {"title": "Kritik am Paper", "doi": "10.1000/xyz", "url": "https://example.org/kritik", "score": 5,
           "reason": "Kritik", "venue": "Nature", "peer_reviewed": "yes", "top_tier": True, "citations": 900}
REVIEW = {"title": "A survey of solid electrolytes", "doi": "10.1000/review", "url": "https://example.org/review",
          "score": 4, "reason": "Überblick", "year": "2025", "venue": "Chemical Reviews", "peer_reviewed": "yes",
          "is_review": True}


def run_job(settings, responses, **opts):
    request = EpisodeRequest(topic="Festkörperbatterien",
                             options=EpisodeOptions(length=Length.kurz, research_depth=ResearchDepth.quick, **opts))
    job = create_job(settings, request, PromptStore(settings.prompts_dir))
    claude = FakeClaude(responses)
    ctx = load_context(job, settings, claude=claude, tts=FakeTTS())
    ctx.download_options = mock_downloads()
    return job, claude, run_pipeline(ctx)


def test_filters_and_merge():
    merged = merge_candidates({"t1": [Candidate(**{**JOURNAL, "peer_reviewed": "unknown", "venue": ""})],
                               "t2": [Candidate(**JOURNAL)]})
    assert merged[0].peer_reviewed == "yes" and merged[0].venue == "Nature" and merged[0].citations == 900
    pre, jour = Candidate(**PREPRINT), Candidate(**JOURNAL)
    assert passes_filter(pre, SourceFilter.all) and not passes_filter(pre, SourceFilter.peer_reviewed)
    assert passes_filter(jour, SourceFilter.top) and passes_filter(jour, SourceFilter.peer_reviewed)


async def test_peer_review_filter_keeps_only_reviewed_sources(settings):
    responses = default_responses()
    responses["ScoutResult"] = [{"candidates": [PREPRINT, JOURNAL]}] * 3
    job, claude, pipeline = run_job(settings, responses, source_filter=SourceFilter.peer_reviewed)
    await pipeline
    (select,) = claude.calls_for("Selection")
    assert "Only peer-reviewed sources" in select.prompt and "Ein Paper" not in select.prompt.split("# Candidates")[1]
    assert "Venue: Nature" in select.prompt and "900 citations" in select.prompt
    assert "Only peer-reviewed sources" in claude.calls_for("ScoutResult")[0].prompt
    log = [json.loads(line) for line in (job / "log.jsonl").read_text().splitlines()]
    (entry,) = [e for e in log if e["event"] == "source_filter"]
    assert entry["kept"] == 1 and entry["removed"][0].startswith("Ein Paper")


async def test_filter_without_matches_fails_clearly(settings):
    responses = default_responses()
    responses["ScoutResult"] = [{"candidates": [PREPRINT]}] * 3
    _, _, pipeline = run_job(settings, responses, source_filter=SourceFilter.top)
    with pytest.raises(PodcastError, match="Keine der 1 gefundenen Quellen ist von einer Top-Konferenz"):
        await pipeline


async def test_overview_mode_searches_and_reads_reviews_first(settings):
    responses = default_responses()
    responses["ScoutResult"] = [{"candidates": [PREPRINT, JOURNAL, REVIEW]}] * 3
    responses["Selection"] = [{"focus": "F", "selected": [{"id": "arxiv:2401.00001", "reason": "Kern"}]}]
    job, claude, pipeline = run_job(settings, responses, research_mode="overview")
    await pipeline
    plan = json.loads((job / "plan.json").read_text())
    assert any("review" in t["title"].lower() for t in plan["tasks"])  # a review scout is guaranteed
    assert "Recent review articles and surveys" in claude.calls_for("ResearchPlan")[0].prompt
    selection = json.loads((job / "selection.json").read_text())
    assert selection["selected"][0]["id"] == "doi:10.1000/review"  # the recent review is read first


def test_series_graph():
    jobs = {"a": SimpleNamespace(status="done", display_title="Grundlagen <1>")}
    linear = {"id": "s", "episodes": ["a"], "plan": {"assigned": {}, "items": [
        {"title": "Zwei", "builds_on": [1]}, {"title": "Drei", "builds_on": [2]}]}}
    graph = series_graph.render(linear, jobs)
    assert graph and not graph["branched"] and "Grundlagen &lt;1&gt;" in graph["svg"]
    assert 'href="/episodes/a"' in graph["svg"] and graph["svg"].count('class="edge"') == 2
    branched = {**linear, "plan": {"assigned": {}, "items": [
        {"title": "Zwei", "builds_on": [1]}, {"title": "Drei", "builds_on": [1]}, {"title": "Vier", "builds_on": [2, 3, 9]}]}}
    graph = series_graph.render(branched, jobs)
    assert graph["branched"] and graph["svg"].count('class="edge"') == 4  # unknown part 9 ignored
    assert series_graph.render({**linear, "plan": {"items": [{"title": "x", "builds_on": []}]}}, jobs) is None
