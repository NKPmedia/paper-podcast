"""Stage 6: remember the episode and suggest follow-ups → memory.json.

Claude summarizes the finished script twice (a one-line mini summary that every
later prompt sees, and a longer reference summary that a later script writer reads
only when it refers back), names the key concepts and suggests up to five
follow-up episodes. The episode is already finished, so a failure here is logged
and does not fail the job; "Neu erzeugen ab Verknüpfung" runs this step again.
"""

from __future__ import annotations

import json

from app import library
from app.claude import ClaudeCall
from app.models import MAX_FOLLOW_UPS, EpisodeMemory, Script, Source, json_schema
from app.prompts import render_stage

NAME = "memory"
DESCRIPTION = "Claude fasst die Folge zusammen und schlägt Folgeepisoden vor"

MAX_TURNS = 4
MINI_CHARS = 300


def is_done(ctx) -> bool:
    return ctx.path(library.MEMORY_FILE).exists()


def dialogue_text(script: Script, names: dict[str, str]) -> str:
    parts = []
    for chapter in script.chapters:
        parts.append(f"## {chapter.title}")
        parts.extend(f"{names[line.speaker]}: {line.text}" for line in chapter.lines)
    return "\n\n".join(parts)


def _clean(memory: EpisodeMemory) -> dict:
    data = memory.model_dump()
    data["mini_summary"] = " ".join(memory.mini_summary.split())[:MINI_CHARS]
    data["key_concepts"] = [c.strip() for c in memory.key_concepts if c.strip()][:10]
    data["follow_ups"] = [
        {k: " ".join(getattr(f, k).split()) for k in ("title", "topic", "why")}
        for f in memory.follow_ups if f.topic.strip()
    ][:MAX_FOLLOW_UPS]
    return data


async def run(ctx) -> None:
    script = Script.model_validate_json(ctx.path("script.json").read_text(encoding="utf-8"))
    sources = [Source(**s) for s in json.loads(ctx.path("sources.json").read_text(encoding="utf-8"))]
    opts = ctx.request.options
    known = library.entries(ctx.settings.episodes_dir, exclude=ctx.job_dir.name)
    prompt = render_stage(
        "memory",
        topic=ctx.request.topic,
        title=script.title,
        summary=script.summary,
        dialogue=dialogue_text(script, {"host": ctx.prompt_context["host_name"],
                                        "expert": ctx.prompt_context["expert_name"]}),
        sources=sources,
        library=known,
        max_follow_ups=MAX_FOLLOW_UPS,
        language_name=opts.language.english_name,
    )
    model = ctx.settings.script_model
    try:
        result = await ctx.claude.run(ClaudeCall(
            prompt=prompt, cwd=ctx.job_dir, tools=[], system_append=ctx.blocks["system"],
            output_schema=json_schema(EpisodeMemory), max_turns=MAX_TURNS, model=model,
        ))
        memory = _clean(EpisodeMemory.model_validate(result.structured))
    except Exception as exc:  # the audio is done; never fail the episode here
        ctx.log.write("memory_failed", stage=NAME, error=f"{type(exc).__name__}: {exc}"[:2000])
        return
    ctx.log_claude(NAME, model, result, follow_ups=len(memory["follow_ups"]))
    known_ids = {e["id"] for e in known}
    memory["references"] = [r.model_dump() for r in script.episode_references if r.id in known_ids]
    memory["follows"] = opts.follows if library.episode(ctx.settings.episodes_dir, opts.follows) else ""
    memory["language"] = opts.language.value
    ctx.path(library.MEMORY_FILE).write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
