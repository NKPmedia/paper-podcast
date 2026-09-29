"""Command line entry point for the core pipeline.

    python -m app.cli new "Neue Festkörperbatterien" --length kurz --depth quick
    python -m app.cli resume <job_dir> [--from-stage script]
    python -m app.cli prompts                # list prompt blocks
    python -m app.cli skills                 # list skills
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.config import get_settings
from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.pipeline import STAGE_NAMES, create_job, load_context, run_pipeline
from app.prompts import BLOCK_DESCRIPTIONS, PromptStore
from app.skills import SkillStore


def _parse_kv(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        key, sep, text = value.partition("=")
        if not sep:
            raise SystemExit(f"Expected BLOCK=TEXT, got {value!r}")
        result[key.strip()] = text
    return result


def _progress(stage: str, description: str) -> None:
    print(f"==> [{stage}] {description}", flush=True)


async def _run(job_dir: Path, from_stage: str | None) -> None:
    settings = get_settings()
    ctx = load_context(job_dir, settings)
    mp3 = await run_pipeline(ctx, progress=_progress, from_stage=from_stage)
    print(f"Fertig: {mp3}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paper-podcast")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="Create and generate a new episode")
    new.add_argument("topic", help="Thema / Paper-Beschreibung")
    new.add_argument("--length", choices=[x.value for x in Length], default=Length.mittel.value)
    new.add_argument("--depth", choices=[x.value for x in ResearchDepth], default=ResearchDepth.medium.value)
    new.add_argument("--extra", default="", help="Zusätzliche Wünsche für diese Episode")
    new.add_argument("--block", action="append", default=[], metavar="BLOCK=TEXT",
                     help="Text an einen Prompt-Block anhängen (mehrfach möglich)")
    new.add_argument("--skill", action="append", default=[], help="Zusätzlichen Skill aktivieren")

    resume = sub.add_parser("resume", help="Continue or partially re-run an existing episode")
    resume.add_argument("job_dir", type=Path)
    resume.add_argument("--from-stage", choices=STAGE_NAMES)

    sub.add_parser("prompts", help="List prompt blocks")
    sub.add_parser("skills", help="List available skills")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    prompts = PromptStore(settings.prompts_dir)

    if args.command == "prompts":
        for name in prompts.names():
            marker = "*" if prompts.is_overridden(name) else " "
            print(f"{marker} {name:14} {BLOCK_DESCRIPTIONS.get(name, '')}")
        print("\n* = angepasst in", settings.prompts_dir)
        return 0

    if args.command == "skills":
        store = SkillStore(settings.skills_dir, settings.data_dir / "skills.json")
        for skill in store.all().values():
            origin = "override" if skill.overridden else ("bundled" if skill.bundled else "user")
            print(f"{skill.name:26} [{origin}] {skill.description[:90]}")
        return 0

    if args.command == "new":
        request = EpisodeRequest(
            topic=args.topic,
            options=EpisodeOptions(
                length=Length(args.length),
                research_depth=ResearchDepth(args.depth),
                extra_instructions=args.extra,
                block_overrides=_parse_kv(args.block),
                extra_skills=args.skill,
            ),
        )
        job_dir = create_job(settings, request, prompts)
        print(f"Episode: {job_dir}")
        from_stage = None
    else:
        job_dir, from_stage = args.job_dir, args.from_stage

    try:
        asyncio.run(_run(job_dir, from_stage))
    except KeyboardInterrupt:
        print(f"\nAbgebrochen. Fortsetzen mit: python -m app.cli resume {job_dir}")
        return 130
    except Exception as exc:
        print(f"Fehler: {exc}\nFortsetzen mit: python -m app.cli resume {job_dir}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
