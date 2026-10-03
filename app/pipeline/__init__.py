"""Episode generation pipeline: research → script → tts → audio.

Every stage writes its artifacts into the job directory and is skipped when they
already exist, so an interrupted job resumes where it stopped and a single stage
can be re-run with ``from_stage``.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.claude import AgentSDKRunner, ClaudeRunner
from app.config import Settings
from app.errors import NeedsInput
from app.models import EpisodeRequest
from app.prompts import PromptStore
from app.skills import SkillStore
from app.tts import TTSProvider, make_tts

ProgressCallback = Callable[[str, str], Awaitable[None] | None]


class EpisodeLog:
    def __init__(self, path: Path):
        self.path = path

    def write(self, event: str, **data) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class EpisodeContext:
    job_dir: Path
    request: EpisodeRequest
    blocks: dict[str, str]
    prompt_context: dict
    settings: Settings
    skills: SkillStore
    claude: ClaudeRunner
    tts: TTSProvider
    # Extra keyword arguments for app.papers.download_all (tests inject an HTTP client).
    download_options: dict = field(default_factory=dict)
    progress: ProgressCallback | None = None
    # Called with the episode title as soon as the AI has chosen one (research, then script).
    on_title: Callable[[str], Awaitable[None] | None] | None = None
    log: EpisodeLog = field(init=False)

    def __post_init__(self):
        self.log = EpisodeLog(self.job_dir / "log.jsonl")

    async def notify(self, stage: str, message: str) -> None:
        self.log.write("progress", stage=stage, message=message)
        if self.progress:
            maybe = self.progress(stage, message)
            if maybe is not None:
                await maybe

    def log_claude(self, stage: str, model: str, result, **extra) -> None:
        """One ``claude`` log entry per call: model, cost, turns, tokens, skills."""
        self.log.write(
            "claude", stage=stage, model=model, cost_usd=result.cost_usd, turns=result.num_turns,
            tokens=getattr(result, "tokens", {}) or {}, skills_used=result.skills_used, **extra,
        )

    async def set_title(self, title: str) -> None:
        title = " ".join((title or "").split())[:120]
        if title and self.on_title:
            maybe = self.on_title(title)
            if maybe is not None:
                await maybe

    def path(self, name: str) -> Path:
        return self.job_dir / name


def current_title(job_dir: Path) -> str:
    """The best title known so far: from the script, else from the research notes."""
    script = job_dir / "script.json"
    if script.exists():
        return json.loads(script.read_text(encoding="utf-8")).get("title", "")
    research = job_dir / "research.md"
    if research.exists():
        first = research.read_text(encoding="utf-8").split("\n", 1)[0]
        return first.removeprefix("# ").strip()
    return ""


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text.replace("ß", "ss")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "episode"


def prompt_context(settings: Settings, request: EpisodeRequest) -> dict:
    minutes = request.options.length.minutes
    return {
        "podcast_name": settings.podcast_name,
        "host_name": settings.host_name,
        "expert_name": settings.expert_name,
        "minutes": minutes,
        "target_words": minutes * settings.words_per_minute,
        "research_depth": request.options.research_depth.value,
        "language": request.options.language.value,
        "language_name": request.options.language.english_name,
        "handout": request.options.handout,
    }


def create_job(settings: Settings, request: EpisodeRequest, prompts: PromptStore) -> Path:
    """Create a job directory with a frozen copy of the request and resolved prompts."""
    ctx = prompt_context(settings, request)
    blocks = prompts.resolve(ctx, request.options.block_overrides)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_dir = settings.episodes_dir / f"{stamp}-{slugify(request.topic)}"
    job_dir.mkdir(parents=True, exist_ok=False)
    (job_dir / "request.json").write_text(
        json.dumps(
            {"request": request.model_dump(mode="json"), "context": ctx, "blocks": blocks},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return job_dir


def load_context(
    job_dir: Path,
    settings: Settings,
    claude: ClaudeRunner | None = None,
    tts: TTSProvider | None = None,
) -> EpisodeContext:
    data = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    request = EpisodeRequest.model_validate(data["request"])
    return EpisodeContext(
        job_dir=job_dir,
        request=request,
        blocks=data["blocks"],
        prompt_context=data["context"],
        settings=settings,
        skills=SkillStore(settings.skills_dir, settings.data_dir / "skills.json"),
        claude=claude or AgentSDKRunner(),
        tts=tts or make_tts(settings, request.options.language.value),
    )


def _stages():
    from app.pipeline import audio, handout, research, script, speech

    return [research, script, handout, speech, audio]


STAGE_NAMES = ["research", "script", "handout", "tts", "audio"]
# The file whose existence marks a stage as finished.
STAGE_ARTIFACTS = {
    "research": "research.md",
    "script": "script.json",
    "handout": "handout.pdf",
    "tts": "clips/manifest.json",
    "audio": "episode.mp3",
}


def stages_for(handout: bool) -> list[str]:
    """The stages an episode actually runs (the handout is optional)."""
    return [s for s in STAGE_NAMES if handout or s != "handout"]


async def run_pipeline(
    ctx: EpisodeContext,
    progress: ProgressCallback | None = None,
    from_stage: str | None = None,
) -> Path:
    if from_stage is not None and from_stage not in STAGE_NAMES:
        raise ValueError(f"Unknown stage {from_stage!r}; choose from {STAGE_NAMES}")
    ctx.progress = progress
    force = False
    for stage in _stages():
        force = force or stage.NAME == from_stage
        if hasattr(stage, "enabled") and not stage.enabled(ctx):
            continue  # optional stage switched off for this episode
        if not force and stage.is_done(ctx):
            ctx.log.write("stage_skipped", stage=stage.NAME)
            continue
        if force and hasattr(stage, "reset"):
            stage.reset(ctx)  # drop intermediate artifacts so nothing stale is reused
        await ctx.notify(stage.NAME, stage.DESCRIPTION)
        ctx.log.write("stage_start", stage=stage.NAME)
        started = time.monotonic()
        try:
            await stage.run(ctx)
        except NeedsInput as exc:
            ctx.log.write("waiting", stage=stage.NAME, questions=len(exc.questions))
            raise
        except Exception as exc:
            ctx.log.write("stage_error", stage=stage.NAME, error=f"{type(exc).__name__}: {exc}")
            raise
        ctx.log.write("stage_done", stage=stage.NAME, seconds=round(time.monotonic() - started, 1))
    return ctx.path("episode.mp3")
