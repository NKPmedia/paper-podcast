"""Stage 2: Claude writes the two-person dialogue → script.json (validated)."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.claude import ClaudeCall
from app.models import Script, Source, json_schema
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


def validate(script: Script, target_words: int, handout: bool = False) -> list[str]:
    problems = []
    if len(script.chapters) < 2:
        problems.append("Das Skript braucht mindestens zwei Kapitel.")
    speakers = {line.speaker for _, _, line in script.iter_lines()}
    if speakers != {"host", "expert"}:
        problems.append("Beide Sprecher (host und expert) müssen vorkommen.")
    for ci, li, line in script.iter_lines():
        if not line.text.strip():
            problems.append(f"Kapitel {ci + 1}, Zeile {li + 1} ist leer.")
        elif FORMULA_PATTERN.search(line.text):
            problems.append(
                f"Kapitel {ci + 1}, Zeile {li + 1} enthält eine geschriebene Formel oder Formelzeichen. "
                "Formeln werden nicht vorgelesen; erkläre die Aussage in Worten."
            )
        elif not handout and HANDOUT_PATTERN.search(line.text):
            problems.append(
                f"Kapitel {ci + 1}, Zeile {li + 1} verweist auf ein Handout, es gibt aber keins."
            )
        elif len(line.text) > MAX_LINE_CHARS:
            problems.append(
                f"Kapitel {ci + 1}, Zeile {li + 1} ist zu lang ({len(line.text)} Zeichen); "
                "teile sie in mehrere Wortwechsel auf."
            )
    if not handout and script.handout_items:
        problems.append("`handout_items` muss leer sein, weil es kein Handout gibt.")
    words = script.word_count
    if words < target_words * MIN_LENGTH_RATIO:
        problems.append(f"Das Skript ist zu kurz: {words} Wörter, Ziel sind etwa {target_words}.")
    elif words > target_words * MAX_LENGTH_RATIO:
        problems.append(f"Das Skript ist zu lang: {words} Wörter, Ziel sind etwa {target_words}.")
    return problems


async def run(ctx) -> None:
    opts = ctx.request.options
    skills = ctx.skills.stage_skills(NAME, opts.extra_skills)
    ctx.skills.install(ctx.job_dir, skills)
    notes = ctx.path("research.md").read_text(encoding="utf-8")
    sources = [Source(**s) for s in json.loads(ctx.path("sources.json").read_text(encoding="utf-8"))]
    target_words = ctx.prompt_context["target_words"]

    prompt = render_stage(
        "script",
        blocks=ctx.blocks,
        topic=ctx.request.topic,
        extra_instructions=opts.extra_instructions,
        notes=notes,
        sources=sources,
        **ctx.prompt_context,
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
                model=ctx.settings.claude_model,
                resume=session_id,
            )
        )
        session_id = result.session_id
        try:
            script = Script.model_validate(result.structured)
            problems = validate(script, target_words, ctx.request.options.handout)
        except ValidationError as exc:
            script, problems = None, [f"Das JSON entspricht nicht dem Schema: {exc}"]
        ctx.log.write(
            "claude",
            stage=NAME,
            attempt=attempt,
            cost_usd=result.cost_usd,
            turns=result.num_turns,
            skills_used=result.skills_used,
            words=script.word_count if script else None,
            problems=problems,
        )
        if not problems:
            ctx.path("script.json").write_text(
                script.model_dump_json(indent=2), encoding="utf-8"
            )
            return
        prompt = (
            "Das Skript erfüllt die Anforderungen noch nicht:\n- "
            + "\n- ".join(problems)
            + "\n\nBitte korrigiere das und gib das vollständige, überarbeitete Skript zurück."
        )
    raise RuntimeError("Skript nach mehreren Versuchen ungültig: " + "; ".join(problems))
