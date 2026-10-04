"""Stage 1: research, modelled on multi-agent deep-research systems.

Every step writes its artifact, so an interrupted job resumes where it stopped.

0.  Clarifying questions (``app.pipeline.clarify``)        → ``clarify.json``
1.  Plan: the lead model writes a research brief with key
    questions and one task per scout (effort scales with depth) → ``plan.json``
2.  Scouts (cheap model, parallel, one per task) search and
    rank candidates                                       → ``scouts/<task>.json`` → ``candidates.json``
3.  Selection: the lead model ranks the sources to read   → ``selection.json``
4.  Download the full texts as Markdown (our code)        → ``papers/*.md``, ``papers/index.json``
5.  Reading budget: from the measured length of every paper
    decide how many are read, and which completely         → ``reading.json``
6.  Reading: the lead model reads them (completely or the key
    sections), cross-checks, fills gaps, writes the notes  → ``research.md``, ``sources.json``
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass

from app.claude import ClaudeCall
from app.errors import PodcastError
from app.models import (
    Candidate,
    RankedCandidate,
    ResearchDepth,
    ResearchPlan,
    ResearchResult,
    ScoutResult,
    SelectedPaper,
    Selection,
    json_schema,
)
from app.papers import coverage_note, download_all, normalize_arxiv_id, to_index
from app.pipeline import clarify
from app.prompts import render_stage

NAME = "research"
DESCRIPTION = "Claude recherchiert das Thema"

SCOUT_TOOLS = ["WebSearch", "WebFetch", "Read"]
# Grep lets the main model pull a paper's outline (its Markdown headings) before reading.
READ_TOOLS = ["Read", "Grep", "WebSearch", "WebFetch"]
MAX_CANDIDATES = 30  # merged shortlist shown to the selection
GAP_SEARCHES = 5  # gap-filling searches while reading

# Token estimates for the reading budget (about 4 characters per token).
CHARS_PER_TOKEN = 4
WEBFETCH_TOKENS = 4_000  # a paper without full text, read through WebFetch answers


def selective_tokens(tokens: int) -> int:
    """Abstract, introduction, method overview, results and conclusion: roughly a fifth plus a base."""
    return min(tokens, 8_000 + tokens // 5)


@dataclass(frozen=True)
class DepthProfile:
    min_scouts: int
    max_scouts: int  # the plan picks a number in this range, by how complex the topic is
    candidates_per_scout: int
    searches_per_scout: int  # WebSearch + API queries; keeps cheap scouts cheap
    scout_turns: int  # searches + skill/reference reads + a few metadata look-ups + the answer
    shortlist: int  # sources the selection ranks; downloaded in that order
    max_papers: int  # at most this many are read
    read_budget: int  # tokens of paper text the main model reads in total
    main_turns: int  # ~3 Read calls per complete paper + gap searches + the answer, with headroom


# A typical paper is 8k–15k tokens; long ones with appendices reach the 35k cap
# (``paper_max_chars``). One model reads everything in one context window, so the
# budget leaves room for the prompt, the outlines, searches and the notes it writes.
PROFILES = {
    ResearchDepth.quick: DepthProfile(1, 2, 8, 5, 12, shortlist=4, max_papers=3, read_budget=45_000, main_turns=30),
    ResearchDepth.medium: DepthProfile(2, 4, 8, 5, 12, shortlist=7, max_papers=5, read_budget=90_000, main_turns=50),
    ResearchDepth.deep: DepthProfile(3, 6, 10, 7, 14, shortlist=10, max_papers=7, read_budget=130_000, main_turns=70),
}


def depth_hint(depth: ResearchDepth) -> str:
    """Short German description for the web UI and Telegram."""
    p = PROFILES[depth]
    return f"{p.min_scouts}–{p.max_scouts} Scouts, bis {p.max_papers} Paper, Lesebudget ~{p.read_budget // 1000}k Tokens"


def is_done(ctx) -> bool:
    return ctx.path("research.md").exists() and ctx.path("sources.json").exists()


def reset(ctx) -> None:
    """Start the research over; the answers to clarifying questions are kept."""
    for name in ("plan.json", "candidates.json", "selection.json", "reading.json"):
        ctx.path(name).unlink(missing_ok=True)
    for name in ("scouts", "papers"):
        shutil.rmtree(ctx.path(name), ignore_errors=True)


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _log_claude(ctx, step: str, model: str, result, **extra) -> None:
    ctx.log_claude(NAME, model, result, step=step, **extra)


def _common(ctx) -> dict:
    """Prompt variables every research step shares."""
    return {
        "topic": ctx.request.topic,
        "extra_instructions": ctx.request.options.extra_instructions,
        "clarifications": clarify.answers_text(ctx.job_dir),
    }


def load_plan(ctx) -> ResearchPlan | None:
    path = ctx.path("plan.json")
    return ResearchPlan.model_validate(_read_json(path)) if path.exists() else None


# --- 1. Plan --------------------------------------------------------------------------


async def make_plan(ctx, profile: DepthProfile) -> ResearchPlan:
    await ctx.notify(NAME, "Claude plant die Recherche")
    prompt = render_stage("plan", blocks=ctx.blocks, min_scouts=profile.min_scouts, max_scouts=profile.max_scouts,
                          depth=ctx.request.options.research_depth.value, **_common(ctx))
    model = ctx.settings.research_main_model
    result = await ctx.claude.run(ClaudeCall(
        prompt=prompt, cwd=ctx.job_dir, tools=[], system_append=ctx.blocks["system"],
        output_schema=json_schema(ResearchPlan), max_turns=3, model=model,
    ))
    plan = ResearchPlan.model_validate(result.structured)
    if not plan.tasks:
        raise PodcastError("Claude hat keinen Rechercheplan erstellt. Mit „Fortsetzen“ erneut versuchen.",
                           f"plan without tasks: {result.structured}")
    plan.tasks = plan.tasks[: profile.max_scouts]
    plan.key_questions = plan.key_questions[:8]
    _log_claude(ctx, "plan", model, result, scouts=len(plan.tasks), questions=len(plan.key_questions))
    ctx.log.write("plan", stage=NAME, focus=plan.focus, scouts=[t.title for t in plan.tasks],
                  key_questions=plan.key_questions)
    return plan


def task_ids(plan: ResearchPlan) -> list[str]:
    return [f"t{i + 1}" for i in range(len(plan.tasks))]


# --- 2. Scouts -------------------------------------------------------------------


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


async def _run_scout(ctx, task_id: str, task, plan: ResearchPlan, profile: DepthProfile,
                     skills: list[str]) -> list[Candidate]:
    out = ctx.path(f"scouts/{task_id}.json")
    if out.exists():
        return [Candidate(**c) for c in _read_json(out)]
    prompt = render_stage(
        "scout", blocks=ctx.blocks, task=task, plan=plan,
        count=profile.candidates_per_scout, searches=profile.searches_per_scout, **_common(ctx),
    )
    model = ctx.settings.research_scout_model
    result = await ctx.claude.run(ClaudeCall(
        prompt=prompt, cwd=ctx.job_dir, tools=SCOUT_TOOLS, skills=skills, system_append=ctx.blocks["system"],
        output_schema=json_schema(ScoutResult), max_turns=profile.scout_turns, model=model,
    ))
    candidates = ScoutResult.model_validate(result.structured).candidates
    _log_claude(ctx, f"scout:{task.title}", model, result, candidates=len(candidates))
    _write_json(out, [c.model_dump() for c in candidates])
    return candidates


async def run_scouts(ctx, plan: ResearchPlan, profile: DepthProfile, skills: list[str]) -> list[RankedCandidate]:
    ids = task_ids(plan)
    await ctx.notify(NAME, f"{len(ids)} Scout(s) suchen nach Quellen: " + ", ".join(t.title for t in plan.tasks))
    outcomes = await asyncio.gather(
        *(_run_scout(ctx, tid, task, plan, profile, skills) for tid, task in zip(ids, plan.tasks)),
        return_exceptions=True,
    )
    results, failures = {}, {}
    for tid, task, outcome in zip(ids, plan.tasks, outcomes):
        if isinstance(outcome, BaseException):
            failures[task.title] = outcome
            ctx.log.write("scout_failed", stage=NAME, angle=task.title, error=f"{type(outcome).__name__}: {outcome}")
        else:
            results[tid] = outcome
    if not results:
        raise scouts_failed(failures)
    ranked = merge_candidates(results)
    if not ranked:
        raise PodcastError(
            "Die Recherche hat keine passenden Quellen gefunden. Formuliere das Thema konkreter – z.B. mit "
            "Paper-Titel, Autor*innen, arXiv-ID oder Fachbegriffen – und starte die Episode neu.",
            f"{len(results)} scout(s) returned no candidates for the topic: {ctx.request.topic!r}",
        )
    return ranked


def scouts_failed(failures: dict[str, BaseException]) -> PodcastError:
    """One clear message for 'every scout failed', naming the cause (usually the same for all)."""
    reasons = {str(exc) for exc in failures.values()}
    count = len(failures)
    if len(reasons) == 1:
        message = f"Die Recherche ist fehlgeschlagen ({'der Scout' if count == 1 else f'alle {count} Scouts'}). " + reasons.pop()
    else:
        causes = " / ".join(sorted(reason[:160] for reason in reasons)[:3])
        message = f"Die Recherche ist fehlgeschlagen: Alle {count} Scouts brachen ab, aus verschiedenen Gründen: {causes}"
    details = "\n\n".join(
        f"Scout '{angle}': {type(exc).__name__}: {exc}" + (f"\n{exc.details}" if isinstance(exc, PodcastError) and exc.details else "")
        for angle, exc in failures.items()
    )
    return PodcastError(message, details)


# --- 3. Selection ---------------------------------------------------------------------


async def select_papers(ctx, candidates: list[RankedCandidate], plan: ResearchPlan | None,
                        profile: DepthProfile) -> Selection:
    await ctx.notify(NAME, f"Auswahl der Paper aus {len(candidates)} Kandidaten")
    prompt = render_stage(
        "select", candidates=candidates, plan=plan, count=profile.shortlist, max_papers=profile.max_papers,
        budget=profile.read_budget, **_common(ctx),
    )
    model = ctx.settings.research_main_model
    result = await ctx.claude.run(ClaudeCall(
        prompt=prompt, cwd=ctx.job_dir, tools=[], system_append=ctx.blocks["system"],
        output_schema=json_schema(Selection), max_turns=3, model=model,
    ))
    selection = Selection.model_validate(result.structured)
    known = {c.id for c in candidates}
    valid, seen = [], set()
    for paper in selection.selected:
        if paper.id in known and paper.id not in seen:
            valid.append(paper)
            seen.add(paper.id)
    dropped = len(selection.selected) - len(valid)
    selection.selected = valid[: profile.shortlist]
    if not selection.selected:  # model returned only unknown IDs: fall back to the ranking
        selection.selected = [
            SelectedPaper(id=c.id, reason="Picked automatically by relevance score")
            for c in candidates[: profile.shortlist]
        ]
    _log_claude(ctx, "select", model, result, selected=len(selection.selected), dropped=dropped)
    return selection


# --- 5. Reading budget -------------------------------------------------------------------


def plan_reading(papers: list[dict], profile: DepthProfile) -> list[dict]:
    """Decide from the measured lengths how many papers are read, and how.

    ``papers`` are in selection order, each with ``tokens`` (0 without a full text).
    The first readable paper is always read completely (it is the core paper). The
    others are read completely while the budget allows, otherwise selectively, and
    skipped once neither fits or ``max_papers`` is reached.
    """
    used, count, plan = 0, 0, []
    for paper in papers:
        tokens = paper.get("tokens", 0)
        entry = {"id": paper["id"], "title": paper.get("title", ""), "file": paper.get("file", ""),
                 "tokens": tokens}
        if count >= profile.max_papers:
            entry.update(mode="skipped", cost=0, reason=f"Höchstzahl von {profile.max_papers} Papern erreicht")
        elif not paper.get("file"):
            if used + WEBFETCH_TOKENS <= profile.read_budget:
                entry.update(mode="webfetch", cost=WEBFETCH_TOKENS, reason="Kein Volltext – gezielte Webabrufe")
            else:
                entry.update(mode="skipped", cost=0, reason="Lesebudget erschöpft")
        elif count == 0 or used + tokens <= profile.read_budget:
            entry.update(mode="full", cost=tokens,
                         reason="Kernpaper – komplett gelesen" if count == 0 else "Passt ins Budget – komplett gelesen")
        elif used + selective_tokens(tokens) <= profile.read_budget:
            entry.update(mode="selective", cost=selective_tokens(tokens),
                         reason="Zu lang fürs restliche Budget – nur Kernabschnitte")
        else:
            entry.update(mode="skipped", cost=0, reason="Lesebudget erschöpft")
        if entry["mode"] != "skipped":
            used += entry["cost"]
            count += 1
        plan.append(entry)
    return plan


def measure(ctx, papers: list[dict]) -> list[dict]:
    for paper in papers:
        path = ctx.path(paper["file"]) if paper.get("file") else None
        paper["tokens"] = len(path.read_text(encoding="utf-8")) // CHARS_PER_TOKEN if path and path.exists() else 0
    return papers


def papers_for_prompt(job_dir) -> list[dict]:
    """Downloaded full texts for later stages, each with a ``note`` that says what an agent
    does not see of it (cut main text, separate appendix, removed references, read only in part)."""
    index_path = job_dir / "papers" / "index.json"
    if not index_path.exists():
        return []
    reading_path = job_dir / "reading.json"
    modes = {e["id"]: e["mode"] for e in _read_json(reading_path)} if reading_path.exists() else {}
    papers = []
    for paper in _read_json(index_path):
        if paper.get("file") and modes.get(paper["id"]) != "skipped":
            papers.append({**paper, "note": coverage_note(paper, modes.get(paper["id"], ""))})
    return papers


# --- 6. Reading ------------------------------------------------------------------------


async def read_and_write_notes(ctx, candidates, selection, plan, reading, profile, skills) -> ResearchResult:
    read = [e for e in reading if e["mode"] != "skipped"]
    full = sum(1 for e in read if e["mode"] == "full")
    tokens = sum(e["cost"] for e in read)
    size = f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)
    await ctx.notify(NAME, f"Claude liest {len(read)} Paper ({full} komplett, ~{size} Tokens)")
    by_id = {c.id: c for c in candidates}
    index = {p["id"]: p for p in _read_json(ctx.path("papers/index.json"))} if ctx.path("papers/index.json").exists() else {}
    read_ids = {e["id"] for e in read}
    prompt = render_stage(
        "read", blocks=ctx.blocks, plan=plan, focus=selection.focus,
        papers=[(e, by_id[e["id"]], coverage_note(index.get(e["id"], {}))) for e in read],
        others=[c for c in candidates if c.id not in read_ids][:15],
        searches=GAP_SEARCHES, language_name=ctx.request.options.language.english_name, **_common(ctx),
    )
    model = ctx.settings.research_main_model
    result = await ctx.claude.run(ClaudeCall(
        prompt=prompt, cwd=ctx.job_dir, tools=READ_TOOLS, skills=skills, system_append=ctx.blocks["system"],
        output_schema=json_schema(ResearchResult), max_turns=profile.main_turns, model=model,
    ))
    research = ResearchResult.model_validate(result.structured)
    _log_claude(ctx, "read", model, result, sources=len(research.sources))
    return research


# --- Stage entry point ----------------------------------------------------------------


async def run(ctx) -> None:
    opts = ctx.request.options
    profile = PROFILES[opts.research_depth]
    skills = ctx.skills.stage_skills(NAME, opts.extra_skills)
    ctx.skills.install(ctx.job_dir, skills)
    await clarify.run(ctx, NAME)  # may pause the job until the listener answers

    plan = load_plan(ctx)
    if ctx.path("candidates.json").exists():
        candidates = [RankedCandidate(**c) for c in _read_json(ctx.path("candidates.json"))]
    else:
        if plan is None:
            plan = await make_plan(ctx, profile)
            _write_json(ctx.path("plan.json"), plan.model_dump())
        candidates = await run_scouts(ctx, plan, profile, skills)
        _write_json(ctx.path("candidates.json"), [c.model_dump() for c in candidates])

    if ctx.path("selection.json").exists():
        selection = Selection.model_validate(_read_json(ctx.path("selection.json")))
    else:
        selection = await select_papers(ctx, candidates, plan, profile)
        _write_json(ctx.path("selection.json"), selection.model_dump())

    by_id = {c.id: c for c in candidates}
    if ctx.path("papers/index.json").exists():
        papers = _read_json(ctx.path("papers/index.json"))
    else:
        await ctx.notify(NAME, f"{len(selection.selected)} Paper werden heruntergeladen und vermessen")
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

    if ctx.path("reading.json").exists():
        reading = _read_json(ctx.path("reading.json"))
    else:
        reading = plan_reading(measure(ctx, papers), profile)
        _write_json(ctx.path("reading.json"), reading)
        chosen = [e for e in reading if e["mode"] != "skipped"]
        ctx.log.write("reading", stage=NAME, budget=profile.read_budget, used=sum(e["cost"] for e in chosen),
                      papers=[{k: e[k] for k in ("title", "tokens", "mode", "reason")} for e in reading])

    research = await read_and_write_notes(ctx, candidates, selection, plan, reading, profile, skills)
    await ctx.set_title(research.title_suggestion)
    ctx.path("research.md").write_text(
        f"# {research.title_suggestion}\n\n{research.notes.strip()}\n\n"
        f"## Podcast material\n\n{research.podcast_material.strip()}\n",
        encoding="utf-8",
    )
    _write_json(ctx.path("sources.json"), [s.model_dump() for s in research.sources])
