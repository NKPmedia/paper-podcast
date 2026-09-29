"""Stage 1: Claude researches the topic on the web → research.md + sources.json."""

from __future__ import annotations

import json

from app.claude import ClaudeCall
from app.models import ResearchResult, json_schema
from app.prompts import render_stage

NAME = "research"
DESCRIPTION = "Claude recherchiert das Thema"

TOOLS = ["WebSearch", "WebFetch", "Read"]


def is_done(ctx) -> bool:
    return ctx.path("research.md").exists() and ctx.path("sources.json").exists()


async def run(ctx) -> None:
    opts = ctx.request.options
    skills = ctx.skills.stage_skills(NAME, opts.extra_skills)
    ctx.skills.install(ctx.job_dir, skills)
    prompt = render_stage(
        "research",
        blocks=ctx.blocks,
        topic=ctx.request.topic,
        extra_instructions=opts.extra_instructions,
    )
    result = await ctx.claude.run(
        ClaudeCall(
            prompt=prompt,
            cwd=ctx.job_dir,
            tools=TOOLS,
            skills=skills,
            system_append=ctx.blocks["system"],
            output_schema=json_schema(ResearchResult),
            max_turns=ctx.settings.claude_max_turns_research[opts.research_depth.value],
            model=ctx.settings.claude_model,
        )
    )
    research = ResearchResult.model_validate(result.structured)
    ctx.log.write(
        "claude",
        stage=NAME,
        cost_usd=result.cost_usd,
        turns=result.num_turns,
        skills_used=result.skills_used,
        sources=len(research.sources),
    )
    ctx.path("research.md").write_text(
        f"# {research.title_suggestion}\n\n{research.notes.strip()}\n", encoding="utf-8"
    )
    ctx.path("sources.json").write_text(
        json.dumps([s.model_dump() for s in research.sources], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
