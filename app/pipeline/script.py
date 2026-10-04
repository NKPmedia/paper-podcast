"""Stage 2: Claude writes the two-person dialogue → script.json (validated)."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app import library
from app.claude import ClaudeCall
from app.models import Script, Source, json_schema
from app.errors import PodcastError
from app.pipeline import clarify
from app.pipeline.research import papers_for_prompt
from app.prompts import render_stage

NAME = "script"
DESCRIPTION = "Claude schreibt das Skript"

TOOLS = ["Read"]
MAX_ATTEMPTS = 3
MIN_LENGTH_RATIO = 0.7
MAX_LENGTH_RATIO = 1.4
MAX_LINE_CHARS = 900

# Written math that must never reach the TTS (spoken explanations are fine).
FORMULA_PATTERN = re.compile(r"[=^∑∫√≤≥≈∂∇]|\\[a-z]+|\b[a-zA-Z]_\{?[a-zA-Z0-9]")
HANDOUT_PATTERN = re.compile(r"handout", re.IGNORECASE)


def is_done(ctx) -> bool:
    return ctx.path("script.json").exists()


def validate(script: Script, target_words: int, handout: bool = False,
             known_episodes: set[str] | None = None) -> list[str]:
    problems = []
    unknown = [r.id for r in script.episode_references if r.id not in (known_episodes or set())]
    if unknown:
        problems.append(
            f"`episode_references` names unknown episodes ({', '.join(unknown)}); use only IDs from the "
            "list of earlier episodes, or leave it empty."
        )
    if len(script.chapters) < 2:
        problems.append("The script needs at least two chapters.")
    speakers = {line.speaker for _, _, line in script.iter_lines()}
    if speakers != {"host", "expert"}:
        problems.append("Both speakers (host and expert) must appear.")
    for ci, li, line in script.iter_lines():
        if not line.text.strip():
            problems.append(f"Chapter {ci + 1}, line {li + 1} is empty.")
        elif FORMULA_PATTERN.search(line.text):
            problems.append(
                f"Chapter {ci + 1}, line {li + 1} contains a written formula or math symbols. "
                "Formulas are never read out; explain the statement in words."
            )
        elif not handout and HANDOUT_PATTERN.search(line.text):
            problems.append(
                f"Chapter {ci + 1}, line {li + 1} refers to a handout, but there is none."
            )
        elif len(line.text) > MAX_LINE_CHARS:
            problems.append(
                f"Chapter {ci + 1}, line {li + 1} is too long ({len(line.text)} characters); "
                "split it into several exchanges."
            )
    if not handout and script.handout_items:
        problems.append("`handout_items` must be empty because there is no handout.")
    words = script.word_count
    if words < target_words * MIN_LENGTH_RATIO:
        problems.append(f"The script is too short: {words} words, the target is about {target_words}.")
    elif words > target_words * MAX_LENGTH_RATIO:
        problems.append(f"The script is too long: {words} words, the target is about {target_words}.")
    return problems


async def run(ctx) -> None:
    opts = ctx.request.options
    skills = ctx.skills.stage_skills(NAME, opts.extra_skills)
    ctx.skills.install(ctx.job_dir, skills)
    notes = ctx.path("research.md").read_text(encoding="utf-8")
    sources = [Source(**s) for s in json.loads(ctx.path("sources.json").read_text(encoding="utf-8"))]
    target_words = ctx.prompt_context["target_words"]
    papers = papers_for_prompt(ctx.job_dir)  # with notes on cut text, appendix and reading depth
    # Earlier episodes: only their mini summaries go into the prompt, the long ones are files in library/.
    earlier = library.snapshot(ctx.job_dir, ctx.settings.episodes_dir)

    prompt = render_stage(
        "script",
        blocks=ctx.blocks,
        topic=ctx.request.topic,
        extra_instructions=opts.extra_instructions,
        notes=notes,
        sources=sources,
        papers=papers,
        clarifications=clarify.answers_text(ctx.job_dir),
        library=earlier,
        **library.prompt_context(ctx.settings, ctx.job_dir.name, opts),
        **{**ctx.prompt_context, "language_name": opts.language.english_name},
    )
    session_id = None
    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await ctx.claude.run(
            ClaudeCall(
                prompt=prompt,
                cwd=ctx.job_dir,
                tools=TOOLS,
                skills=skills,
                system_append=ctx.blocks["system"],
                output_schema=json_schema(Script),
                max_turns=ctx.settings.claude_max_turns_script,
                model=ctx.settings.script_model,
                resume=session_id,
            )
        )
        session_id = result.session_id
        try:
            script = Script.model_validate(result.structured)
            problems = validate(script, target_words, opts.handout, {e["id"] for e in earlier})
        except ValidationError as exc:
            script, problems = None, [f"The JSON does not match the schema: {exc}"]
        ctx.log_claude(
            NAME, ctx.settings.script_model, result,
            attempt=attempt, words=script.word_count if script else None, problems=problems,
        )
        if not problems:
            ctx.path("script.json").write_text(
                script.model_dump_json(indent=2), encoding="utf-8"
            )
            await ctx.set_title(script.title)
            return
        prompt = (
            "The script does not meet the requirements yet:\n- "
            + "\n- ".join(problems)
            + "\n\nPlease fix this and return the complete, revised script."
        )
    raise PodcastError(
        f"Claude hat nach {MAX_ATTEMPTS} Versuchen kein gültiges Skript geliefert. Mit „Fortsetzen“ erneut "
        "versuchen; hilft das nicht, die Länge ändern oder die Sprechregeln unter Prompts prüfen.",
        "Remaining problems:\n- " + "\n- ".join(problems),
    )
