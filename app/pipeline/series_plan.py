"""Series planning: after a deep research of the whole field, Claude plans a series.

A planning job runs two stages: the normal research stage (depth ``deep``, with a
hint that it maps a field for a whole series) and this one. Its input is
``series_plan_request.json`` in the job directory::

    {"series_id": "...", "count": 0 (Claude decides) or the number of new parts,
     "episode_options": {length, language, audience, handout, tts, research_depth},
     "revisions": [{"instruction": "...", "at": "...", "applied": false}]}

The result is ``series_plan.json`` and the plan stored in the series
(``app.series``). "Planung anpassen" adds a revision and re-runs only this stage:
the research is reused, the parts that already exist stay fixed, and only the
open parts are planned again.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app import library
from app.claude import ClaudeCall
from app.errors import PodcastError
from app.models import MAX_PLANNED, MIN_PLANNED, SeriesPlanResult, Source, json_schema
from app.pipeline.research import papers_for_prompt
from app.prompts import render_stage
from app.series import SeriesStore

NAME = "series_plan"
DESCRIPTION = "Claude plant die Reihe"
REQUEST_FILE = "series_plan_request.json"
RESULT_FILE = "series_plan.json"
TOOLS = ["Read", "Grep", "WebSearch", "WebFetch"]
MAX_TURNS = 40
MAX_SEARCHES = 5
MAX_ATTEMPTS = 2

# Appended to the research request of a planning job.
RESEARCH_HINT = (
    "This research prepares a whole podcast series, not a single episode. Map the field broadly: its "
    "foundations, the main lines of work and how they relate, the key papers of each, the current state of the "
    "art, open problems and controversies, so that it can be split into a sequence of episodes that build on "
    "each other."
)


def load_request(job_dir: Path) -> dict:
    return json.loads((job_dir / REQUEST_FILE).read_text(encoding="utf-8"))


def save_request(job_dir: Path, data: dict) -> None:
    (job_dir / REQUEST_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_revisions(job_dir: Path) -> list[str]:
    path = job_dir / REQUEST_FILE
    if not path.exists():
        return []
    return [r["instruction"] for r in load_request(job_dir).get("revisions", []) if not r.get("applied")]


def is_done(ctx) -> bool:
    return ctx.path(RESULT_FILE).exists() and not pending_revisions(ctx.job_dir)


def _existing_parts(ctx, series: dict) -> list[dict]:
    """The parts the series already has (fixed), with what is known about each."""
    parts = []
    assigned = (series.get("plan") or {}).get("assigned") or {}
    for i, job_id in enumerate(series["episodes"]):
        entry = library.episode(ctx.settings.episodes_dir, job_id)
        planned = assigned.get(job_id) or {}
        title = (entry or {}).get("title") or planned.get("title") or job_id
        parts.append({"part": i + 1, "title": title, "done": bool(entry),
                      "summary": (entry or {}).get("mini") or planned.get("goal", "")})
    return parts


def problems(result: SeriesPlanResult, count: int, revising: bool, existing: int) -> list[str]:
    found = []
    n = len(result.episodes)
    if count and not revising and n != count:
        found.append(f"Plan exactly {count} new parts (you returned {n}).")
    elif not MIN_PLANNED <= n <= MAX_PLANNED and not (revising and n >= 1):
        found.append(f"Plan between {MIN_PLANNED} and {MAX_PLANNED} new parts (you returned {n}).")
    for i, ep in enumerate(result.episodes):
        if not ep.topic.strip():
            found.append(f"Planned part {existing + i + 1} has no topic.")
    return found


def _clean(result: SeriesPlanResult, existing: int) -> dict:
    data = result.model_dump()
    for i, ep in enumerate(data["episodes"]):
        number = existing + i + 1
        ep["builds_on"] = sorted({b for b in ep["builds_on"] if 1 <= b < number})
        ep["covers"] = [c.strip() for c in ep["covers"] if c.strip()][:8]
        ep["sources"] = [s.strip() for s in ep["sources"] if s.strip()][:6]
    return data


async def run(ctx) -> None:
    request = load_request(ctx.job_dir)
    store = SeriesStore(ctx.settings.series_path, ctx.settings.episodes_dir)
    series = store.get(request["series_id"])
    if series is None:
        raise PodcastError("Die Reihe zu dieser Planung gibt es nicht mehr (sie wurde aufgelöst). Lege unter Reihen "
                           "eine neue Planung an.")
    revisions = pending_revisions(ctx.job_dir)
    plan = series.get("plan") or {}
    revising = bool(revisions) and ctx.path(RESULT_FILE).exists()
    existing = _existing_parts(ctx, series)
    sources = [Source(**s) for s in json.loads(ctx.path("sources.json").read_text(encoding="utf-8"))]
    options = request.get("episode_options", {})
    language_name = ctx.request.options.language.english_name
    prompt = render_stage(
        "series_plan",
        goal=ctx.request.topic,
        count=request.get("count", 0),
        min_parts=MIN_PLANNED, max_parts=MAX_PLANNED,
        minutes=ctx.prompt_context["minutes"],
        audience=options.get("audience", "regular"),
        notes=ctx.path("research.md").read_text(encoding="utf-8"),
        sources=sources,
        papers=papers_for_prompt(ctx.job_dir),
        library=[e for e in library.entries(ctx.settings.episodes_dir) if e["id"] not in series["episodes"]],
        series_title=series["title"], series_arc=series.get("arc", ""),
        existing=existing,
        current=(plan.get("items") or []) if revising else [],
        history=plan.get("history") or [],
        revisions=revisions if revising else [],
        searches=MAX_SEARCHES,
        language_name=language_name,
    )
    model = ctx.settings.research_main_model
    session_id, found = None, []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await ctx.claude.run(ClaudeCall(
            prompt=prompt, cwd=ctx.job_dir, tools=TOOLS, system_append=ctx.blocks["system"],
            output_schema=json_schema(SeriesPlanResult), max_turns=MAX_TURNS, model=model, resume=session_id,
            label="Reihenplanung" if attempt == 1 else "Reihenplanung (Nachbesserung)",
        ))
        session_id = result.session_id
        try:
            plan_result = SeriesPlanResult.model_validate(result.structured)
            found = problems(plan_result, request.get("count", 0), revising, len(existing))
        except ValidationError as exc:
            plan_result, found = None, [f"The JSON does not match the schema: {exc}"]
        ctx.log_claude(NAME, model, result, attempt=attempt, planned=len(plan_result.episodes) if plan_result else None,
                       problems=found)
        if not found:
            break
        prompt = "The plan does not meet the requirements yet:\n- " + "\n- ".join(found) + \
                 "\n\nReturn the complete, corrected plan."
    else:
        raise PodcastError(
            f"Claude hat nach {MAX_ATTEMPTS} Versuchen keinen brauchbaren Reihenplan geliefert ({'; '.join(found)}). "
            "Mit „Fortsetzen“ erneut versuchen.", "\n".join(found))

    data = _clean(plan_result, len(existing))
    ctx.path(RESULT_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    store.apply_plan(series["id"], data, revisions)
    for revision in request.get("revisions", []):
        revision["applied"] = True
    request["planned_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_request(ctx.job_dir, request)
    ctx.log.write("series_plan", stage=NAME, parts=len(data["episodes"]), existing=len(existing),
                  revision=revising, title=data["title"])
    await ctx.set_title(data["title"])
