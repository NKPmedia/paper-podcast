"""Stage 1: research in four resumable steps.

1a. Scouts (cheap model, parallel, one per angle) search broadly and return
    ranked candidates → ``scouts/<angle>.json`` → merged ``candidates.json``
1b. Selection (main model) picks the papers to read      → ``selection.json``
1c. Our code downloads the full texts as Markdown         → ``papers/``
1d. The main model reads all of them and writes notes     → ``research.md``, ``sources.json``
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass

from app.claude import ClaudeCall
from app.models import (
    Candidate,
    RankedCandidate,
    ResearchDepth,
    ResearchResult,
    ScoutResult,
    SelectedPaper,
    Selection,
    json_schema,
)
from app.papers import download_all, normalize_arxiv_id, to_index
from app.prompts import render_stage

NAME = "research"
DESCRIPTION = "Claude recherchiert das Thema"

SCOUT_TOOLS = ["WebSearch", "WebFetch", "Read"]
READ_TOOLS = ["Read", "WebSearch", "WebFetch"]
MAX_CANDIDATES = 40

ANGLES = {
    "overview": "Breiter Überblick: die Kernarbeit(en) zum Thema, wichtige Übersichtsartikel und aktuelle Entwicklungen.",
    "background": "Hintergrund und Vorarbeiten: Auf welchen Arbeiten baut das Thema auf? Klassiker, Grundlagen, Übersichtsartikel.",
    "core": "Kernarbeiten zu Methode und Ergebnissen: die zentralen Paper mit den wichtigsten Resultaten und Zahlen.",
    "critique": "Kritik und Einordnung: Replikationen, Grenzen, Gegenpositionen, Folgearbeiten und praktische Anwendungen.",
    "citations": "Zitationsnetz: Finde die Kernarbeit und verfolge über Semantic Scholar, wer sie zitiert (einflussreiche Folgearbeiten) und worauf sie aufbaut.",
    "recent": "Neueste Entwicklungen der letzten 12 bis 24 Monate: aktuelle Preprints und Konferenzbeiträge.",
}


@dataclass(frozen=True)
class DepthProfile:
    angles: tuple[str, ...]
    candidates_per_scout: int
    searches_per_scout: int  # WebSearch + API queries; keeps cheap scouts cheap
    papers: int
    scout_turns: int
    main_turns: int


PROFILES = {
    ResearchDepth.quick: DepthProfile(("overview",), 10, 8, 3, 14, 40),
    ResearchDepth.medium: DepthProfile(("background", "core", "critique"), 12, 6, 6, 12, 80),
    ResearchDepth.deep: DepthProfile(("background", "core", "critique", "citations", "recent"), 15, 8, 10, 16, 150),
}


def is_done(ctx) -> bool:
    return ctx.path("research.md").exists() and ctx.path("sources.json").exists()


def reset(ctx) -> None:
    for name in ("candidates.json", "selection.json"):
        ctx.path(name).unlink(missing_ok=True)
    for name in ("scouts", "papers"):
        shutil.rmtree(ctx.path(name), ignore_errors=True)


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _log_claude(ctx, step: str, model: str, result, **extra) -> None:
    ctx.log.write(
        "claude", stage=NAME, step=step, model=model, cost_usd=result.cost_usd,
        turns=result.num_turns, skills_used=result.skills_used, **extra,
    )


# --- 1a. Scouts -------------------------------------------------------------------


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _find_arxiv_id(c: Candidate) -> str:
    if c.arxiv_id:
        return normalize_arxiv_id(c.arxiv_id)
    for value in (c.doi, c.url, c.pdf_url):
        match = re.search(r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv\.)(\d{4}\.\d{4,5})", value, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def candidate_id(c: Candidate) -> str:
    arxiv = _find_arxiv_id(c)
    if arxiv:
        return f"arxiv:{arxiv}"
    if c.doi:
        return "doi:" + re.sub(r"^https?://(dx\.)?doi\.org/", "", c.doi.strip()).lower()
    return "title:" + _norm_title(c.title).replace(" ", "-")[:80]


def merge_candidates(results: dict[str, list[Candidate]]) -> list[RankedCandidate]:
    """Deduplicate by arXiv ID / DOI / title; keep the best score, fill gaps, record angles."""
    merged: dict[str, RankedCandidate] = {}
    by_title: dict[str, str] = {}
    for angle, candidates in results.items():
        for c in candidates:
            cid = candidate_id(c)
            key = cid if cid in merged else by_title.get(_norm_title(c.title), cid)
            if key not in merged:
                ranked = RankedCandidate(**c.model_dump(), id=cid, found_by=[angle])
                ranked.arxiv_id = _find_arxiv_id(c)
                merged[key] = ranked
                by_title[_norm_title(c.title)] = key
                continue
            existing = merged[key]
            if angle not in existing.found_by:
                existing.found_by.append(angle)
            if c.score > existing.score:
                existing.score, existing.reason = c.score, c.reason
                existing.quote = c.quote or existing.quote
            for field in ("authors", "year", "doi", "url", "pdf_url"):
                if not getattr(existing, field) and getattr(c, field):
                    setattr(existing, field, getattr(c, field))
            if not existing.arxiv_id:
                existing.arxiv_id = _find_arxiv_id(c)
                if existing.arxiv_id and not existing.id.startswith("arxiv:"):
                    existing.id = f"arxiv:{existing.arxiv_id}"
    ranked = sorted(merged.values(), key=lambda r: (r.score, len(r.found_by)), reverse=True)
    return ranked[:MAX_CANDIDATES]


async def _run_scout(ctx, angle: str, profile: DepthProfile, skills: list[str]) -> list[Candidate]:
    out = ctx.path(f"scouts/{angle}.json")
    if out.exists():
        return [Candidate(**c) for c in _read_json(out)]
    opts = ctx.request.options
    prompt = render_stage(
        "scout",
        blocks=ctx.blocks,
        topic=ctx.request.topic,
        extra_instructions=opts.extra_instructions,
        angle=ANGLES[angle],
        count=profile.candidates_per_scout,
        searches=profile.searches_per_scout,
    )
    model = ctx.settings.research_scout_model
    result = await ctx.claude.run(
        ClaudeCall(
            prompt=prompt,
            cwd=ctx.job_dir,
            tools=SCOUT_TOOLS,
            skills=skills,
            system_append=ctx.blocks["system"],
            output_schema=json_schema(ScoutResult),
            max_turns=profile.scout_turns,
            model=model,
        )
    )
    candidates = ScoutResult.model_validate(result.structured).candidates
    _log_claude(ctx, f"scout:{angle}", model, result, candidates=len(candidates))
    _write_json(out, [c.model_dump() for c in candidates])
    return candidates


async def run_scouts(ctx, profile: DepthProfile, skills: list[str]) -> list[RankedCandidate]:
    await ctx.notify(NAME, f"{len(profile.angles)} Scout(s) suchen nach Quellen")
    outcomes = await asyncio.gather(
        *(_run_scout(ctx, angle, profile, skills) for angle in profile.angles),
        return_exceptions=True,
    )
    results = {}
    for angle, outcome in zip(profile.angles, outcomes):
        if isinstance(outcome, BaseException):
            ctx.log.write("scout_failed", stage=NAME, angle=angle, error=f"{type(outcome).__name__}: {outcome}")
        else:
            results[angle] = outcome
    if not results:
        raise RuntimeError("Alle Scouts sind fehlgeschlagen")
    ranked = merge_candidates(results)
    if not ranked:
        raise RuntimeError("Die Scouts haben keine Quellen gefunden")
    return ranked


# --- 1b. Selection ----------------------------------------------------------------


async def select_papers(ctx, candidates: list[RankedCandidate], profile: DepthProfile) -> Selection:
    await ctx.notify(NAME, f"Auswahl der Paper aus {len(candidates)} Kandidaten")
    prompt = render_stage(
        "select",
        topic=ctx.request.topic,
        extra_instructions=ctx.request.options.extra_instructions,
        candidates=candidates,
        count=profile.papers,
    )
    model = ctx.settings.research_main_model
    result = await ctx.claude.run(
        ClaudeCall(
            prompt=prompt,
            cwd=ctx.job_dir,
            tools=[],
            system_append=ctx.blocks["system"],
            output_schema=json_schema(Selection),
            max_turns=3,
            model=model,
        )
    )
    selection = Selection.model_validate(result.structured)
    known = {c.id for c in candidates}
    valid, seen = [], set()
    for paper in selection.selected:
        if paper.id in known and paper.id not in seen:
            valid.append(paper)
            seen.add(paper.id)
    dropped = len(selection.selected) - len(valid)
    selection.selected = valid[: profile.papers]
    if not selection.selected:  # model returned only unknown IDs: fall back to the ranking
        selection.selected = [
            SelectedPaper(id=c.id, reason="Automatisch nach Relevanz gewählt") for c in candidates[: profile.papers]
        ]
    _log_claude(ctx, "select", model, result, selected=len(selection.selected), dropped=dropped)
    return selection


# --- 1d. Deep reading ---------------------------------------------------------------


async def read_and_write_notes(ctx, candidates, selection, papers, profile, skills) -> ResearchResult:
    full = sum(1 for p in papers if p["file"])
    await ctx.notify(NAME, f"Claude liest {len(papers)} Paper ({full} im Volltext, {len(papers) - full} per WebFetch)")
    by_id = {c.id: c for c in candidates}
    selected_ids = {p.id for p in selection.selected}
    prompt = render_stage(
        "read",
        blocks=ctx.blocks,
        topic=ctx.request.topic,
        extra_instructions=ctx.request.options.extra_instructions,
        focus=selection.focus,
        selected=[(p, by_id[p.id], next(x for x in papers if x["id"] == p.id)) for p in selection.selected],
        others=[c for c in candidates if c.id not in selected_ids],
    )
    model = ctx.settings.research_main_model
    result = await ctx.claude.run(
        ClaudeCall(
            prompt=prompt,
            cwd=ctx.job_dir,
            tools=READ_TOOLS,
            skills=skills,
            system_append=ctx.blocks["system"],
            output_schema=json_schema(ResearchResult),
            max_turns=profile.main_turns,
            model=model,
        )
    )
    research = ResearchResult.model_validate(result.structured)
    _log_claude(ctx, "read", model, result, sources=len(research.sources))
    return research


# --- Stage entry point ----------------------------------------------------------------


async def run(ctx) -> None:
    opts = ctx.request.options
    profile = PROFILES[opts.research_depth]
    skills = ctx.skills.stage_skills(NAME, opts.extra_skills)
    ctx.skills.install(ctx.job_dir, skills)

    if ctx.path("candidates.json").exists():
        candidates = [RankedCandidate(**c) for c in _read_json(ctx.path("candidates.json"))]
    else:
        candidates = await run_scouts(ctx, profile, skills)
        _write_json(ctx.path("candidates.json"), [c.model_dump() for c in candidates])

    if ctx.path("selection.json").exists():
        selection = Selection.model_validate(_read_json(ctx.path("selection.json")))
    else:
        selection = await select_papers(ctx, candidates, profile)
        _write_json(ctx.path("selection.json"), selection.model_dump())

    if ctx.path("papers/index.json").exists():
        papers = _read_json(ctx.path("papers/index.json"))
    else:
        await ctx.notify(NAME, f"{len(selection.selected)} Paper werden heruntergeladen")
        by_id = {c.id: c for c in candidates}
        files = await download_all(
            [by_id[p.id] for p in selection.selected],
            ctx.path("papers"),
            max_bytes=ctx.settings.download_max_mb * 1024 * 1024,
            max_chars=ctx.settings.paper_max_chars,
            timeout_s=ctx.settings.download_timeout_s,
            **ctx.download_options,
        )
        papers = to_index(files)
        ctx.log.write(
            "downloads", stage=NAME, ok=sum(1 for p in papers if p["file"]),
            failed=[{"id": p["id"], "error": p["error"][:300]} for p in papers if not p["file"]],
        )
        _write_json(ctx.path("papers/index.json"), papers)

    research = await read_and_write_notes(ctx, candidates, selection, papers, profile, skills)
    ctx.path("research.md").write_text(
        f"# {research.title_suggestion}\n\n{research.notes.strip()}\n", encoding="utf-8"
    )
    _write_json(ctx.path("sources.json"), [s.model_dump() for s in research.sources])
