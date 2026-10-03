"""Command line entry point for the core pipeline.

    python -m app.cli new "Neue Festkörperbatterien" --length kurz --depth quick
    python -m app.cli resume <job_dir> [--from-stage script]
    python -m app.cli enqueue "Thema" [...]  # hand a job to the running server's queue
    python -m app.cli prompts                # list prompt blocks
    python -m app.cli skills                 # list skills
    python -m app.cli set-password           # set or reset the web UI password
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

from app.auth import hash_password
from app.config import get_settings, save_settings
from app.db import JobStore
from app.jobs import JobService
from app.models import EpisodeOptions, EpisodeRequest, Language, Length, ResearchDepth
from app.errors import NeedsInput
from app.pipeline import STAGE_NAMES, clarify, create_job, load_context, run_pipeline
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


def ask_questions(questions: list[dict]) -> list[str]:
    """Ask the clarifying questions in the terminal; Enter on an empty line lets Claude decide."""
    print(f"\nClaude hat {len(questions)} Rückfrage(n). Nummer wählen, eigene Antwort tippen oder leer lassen.")
    answers = []
    for i, question in enumerate(questions, start=1):
        print(f"\n{i}. {question['question']}")
        for k, option in enumerate(question["options"], start=1):
            print(f"   {k}) {option}")
        reply = input("> ").strip()
        if reply.isdigit() and 1 <= int(reply) <= len(question["options"]):
            reply = question["options"][int(reply) - 1]
        elif reply and not question.get("allow_free_text", True):
            reply = ""
        answers.append(reply)
    return answers


async def _run(job_dir: Path, from_stage: str | None) -> None:
    settings = get_settings()
    while True:
        ctx = load_context(job_dir, settings)
        try:
            mp3 = await run_pipeline(ctx, progress=_progress, from_stage=from_stage)
        except NeedsInput as exc:
            if not sys.stdin.isatty():
                raise SystemExit(f"Claude hat Rückfragen, aber es gibt kein Terminal zum Antworten. "
                                 f"Episode mit --no-questions neu starten. ({job_dir})") from exc
            clarify.save_answers(job_dir, ask_questions(exc.questions))
            continue
        print(f"Fertig: {mp3}")
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paper-podcast")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("new", "Create and generate a new episode right here"),
        ("enqueue", "Queue a new episode for the running server"),
    ):
        new = sub.add_parser(command, help=help_text)
        new.add_argument("topic", help="Thema / Paper-Beschreibung")
        new.add_argument("--length", choices=[x.value for x in Length], default=Length.mittel.value)
        new.add_argument("--depth", choices=[x.value for x in ResearchDepth], default=ResearchDepth.medium.value)
        new.add_argument("--handout", action="store_true", help="Handout als PDF erstellen")
        new.add_argument("--no-questions", action="store_true",
                         help="Keine Rückfragen vor der Recherche, auch wenn das Thema unklar ist")
        new.add_argument("--language", choices=[x.value for x in Language], default=None,
                         help="Sprache der Episode (Standard: Einstellung default_language)")
        new.add_argument("--extra", default="", help="Zusätzliche Wünsche für diese Episode")
        new.add_argument("--block", action="append", default=[], metavar="BLOCK=TEXT",
                         help="Text an einen Prompt-Block anhängen (mehrfach möglich)")
        new.add_argument("--skill", action="append", default=[], help="Zusätzlichen Skill aktivieren")

    resume = sub.add_parser("resume", help="Continue or partially re-run an existing episode")
    resume.add_argument("job_dir", type=Path)
    resume.add_argument("--from-stage", choices=STAGE_NAMES)

    sub.add_parser("prompts", help="List prompt blocks")
    sub.add_parser("skills", help="List available skills")
    sub.add_parser("set-password", help="Set or reset the web UI password")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()

    if args.command == "set-password":
        password = getpass.getpass("Neues Passwort: ")
        if len(password) < 10:
            print("Bitte mindestens 10 Zeichen verwenden.", file=sys.stderr)
            return 1
        if getpass.getpass("Wiederholen: ") != password:
            print("Die Passwörter stimmen nicht überein.", file=sys.stderr)
            return 1
        save_settings(settings, {"web_password_hash": hash_password(password)})
        print("Passwort gespeichert.")
        return 0

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

    if args.command in ("new", "enqueue"):
        request = EpisodeRequest(
            topic=args.topic,
            options=EpisodeOptions(
                length=Length(args.length),
                research_depth=ResearchDepth(args.depth),
                handout=args.handout,
                language=Language(args.language or settings.default_language),
                clarify=not args.no_questions,
                extra_instructions=args.extra,
                block_overrides=_parse_kv(args.block),
                extra_skills=args.skill,
            ),
        )
        if args.command == "enqueue":
            job = JobService(settings, JobStore(settings.db_path), prompts).submit(request, origin="cli")
            print(f"In Warteschlange: {job.id}")
            return 0
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
