"""Clarifying questions before the research (first step of the research stage).

Claude looks at the topic and either decides it is clear, or asks up to
``MAX_QUESTIONS`` questions with premade answers (plus optional free text). The
job then pauses (status ``waiting``) until the answers arrive from the web UI,
Telegram or the CLI. Everything lives in ``clarify.json`` in the job directory::

    {"needs_clarification": true, "reason": "...", "questions": [...],
     "answers": null | [{"question": "...", "answer": "..."}], "answered_at": "..."}

The answers are passed to every later Claude call (scouts, selection, reading, script).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.claude import ClaudeCall
from app.errors import NeedsInput
from app.models import MAX_QUESTIONS, ClarificationRequest, json_schema
from app.prompts import render_stage

FILE = "clarify.json"
TOOLS = ["WebSearch", "WebFetch"]
MAX_SEARCHES = 3
MAX_TURNS = 8


def load(job_dir: Path) -> dict | None:
    path = job_dir / FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _save(job_dir: Path, data: dict) -> None:
    (job_dir / FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_questions(job_dir: Path) -> list[dict]:
    """Questions that still wait for answers (empty when none were asked or all are answered)."""
    data = load(job_dir)
    if not data or data.get("answers") is not None:
        return []
    return data.get("questions") or []


def save_answers(job_dir: Path, answers: list[str]) -> list[dict]:
    """Store one answer per question; an empty answer means 'Claude decides'."""
    data = load(job_dir)
    if not data:
        raise ValueError("Für diese Episode gibt es keine Rückfragen.")
    questions = data.get("questions") or []
    answers = [" ".join((a or "").split())[:1000] for a in answers] + [""] * len(questions)
    data["answers"] = [{"question": q["question"], "answer": answers[i]} for i, q in enumerate(questions)]
    data["answered_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save(job_dir, data)
    return data["answers"]


def answers_text(job_dir: Path) -> str:
    """The answered questions as Markdown for the prompts ('' when there are none)."""
    data = load(job_dir)
    if not data or not data.get("answers"):
        return ""
    lines = []
    for item in data["answers"]:
        answer = item["answer"] or "(no preference; decide sensibly yourself)"
        lines.append(f"- Q: {item['question']}\n  A: {answer}")
    return "\n".join(lines)


def _clean(result: ClarificationRequest) -> dict:
    questions = []
    for q in result.questions[:MAX_QUESTIONS]:
        options = [o.strip() for o in q.options if o.strip()][:5]
        if q.question.strip() and (options or q.allow_free_text):
            questions.append({"question": q.question.strip(), "options": options,
                              "allow_free_text": q.allow_free_text or not options})
    needed = bool(result.needs_clarification and questions)
    return {"needs_clarification": needed, "reason": result.reason, "questions": questions if needed else [],
            "answers": None if needed else []}


async def run(ctx, notify_stage: str) -> None:
    """Ask Claude whether to clarify; raise ``NeedsInput`` while answers are missing."""
    opts = ctx.request.options
    data = load(ctx.job_dir)
    if data is None:
        if not opts.clarify or ctx.path("candidates.json").exists():
            return  # switched off, or the research already ran before this step existed
        await ctx.notify(notify_stage, "Claude prüft, ob das Thema eindeutig ist")
        prompt = render_stage(
            "clarify", topic=ctx.request.topic, extra_instructions=opts.extra_instructions,
            max_questions=MAX_QUESTIONS, searches=MAX_SEARCHES, language_name=opts.language.english_name,
        )
        model = ctx.settings.research_main_model
        result = await ctx.claude.run(ClaudeCall(
            prompt=prompt, cwd=ctx.job_dir, tools=TOOLS, system_append=ctx.blocks["system"],
            output_schema=json_schema(ClarificationRequest), max_turns=MAX_TURNS, model=model,
        ))
        data = _clean(ClarificationRequest.model_validate(result.structured))
        ctx.log_claude(notify_stage, model, result, step="clarify", questions=len(data["questions"]))
        _save(ctx.job_dir, data)
        ctx.log.write("clarify", stage=notify_stage, questions=len(data["questions"]), reason=data["reason"])
    if data.get("answers") is None:
        raise NeedsInput(data.get("questions") or [])
