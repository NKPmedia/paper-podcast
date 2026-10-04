"""Job service (create, cancel, retry, delete) and the single background worker.

Every interface (web, Telegram, API, scheduler) creates jobs through ``JobService``;
the ``Worker`` runs one job at a time. Jobs interrupted by a restart are queued
again and resume from the artifacts already on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Callable, Protocol

from app.config import Settings
from app.db import Job, JobStore, now
from app.models import EpisodeRequest
from app.errors import NeedsInput, format_error
from app.pipeline import (
    STAGE_NAMES, EpisodeContext, EpisodeLog, clarify, create_job, current_title, load_context, run_pipeline,
)
from app.prompts import PromptStore

log = logging.getLogger(__name__)

ContextFactory = Callable[[Path], EpisodeContext]


class JobListener(Protocol):
    """Notified about job events: 'started', 'progress', 'waiting', 'done', 'failed', 'cancelled'."""

    async def job_event(self, job: Job, event: str) -> None: ...


def episode_cost(job_dir: Path) -> float | None:
    """Sum of Claude costs recorded in the job log (API-equivalent USD)."""
    log_file = job_dir / "log.jsonl"
    if not log_file.exists():
        return None
    total = 0.0
    for line in log_file.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("event") == "claude" and entry.get("cost_usd"):
            total += entry["cost_usd"]
    return round(total, 4)


class JobError(RuntimeError):
    pass


class JobService:
    def __init__(self, settings: Settings, store: JobStore, prompts: PromptStore):
        self.settings = settings
        self.store = store
        self.prompts = prompts
        self.worker: Worker | None = None

    def job_dir(self, job_id: str) -> Path:
        path = (self.settings.episodes_dir / job_id).resolve()
        if path.parent != self.settings.episodes_dir.resolve():
            raise JobError("invalid job id")
        return path

    def request(self, job_id: str) -> EpisodeRequest:
        data = json.loads((self.job_dir(job_id) / "request.json").read_text(encoding="utf-8"))
        return EpisodeRequest.model_validate(data["request"])

    def submit(self, request: EpisodeRequest, origin: str = "web") -> Job:
        job_dir = create_job(self.settings, request, self.prompts)
        job = self.store.add(job_dir.name, request.topic, origin)
        self._wake()
        return job

    def retry(self, job_id: str, from_stage: str | None = None) -> None:
        job = self._get(job_id)
        if job.active:
            raise JobError("Job läuft bereits")
        if from_stage is not None and from_stage not in STAGE_NAMES:
            raise JobError(f"Unbekannte Stufe: {from_stage}")
        self.store.update(job_id, status="queued", error="", message="", from_stage=from_stage, finished_at=None)
        self._wake()

    def answer(self, job_id: str, answers: list[str]) -> None:
        """Store the answers to the clarifying questions and queue the job again."""
        job = self._get(job_id)
        if job.status != "waiting":
            raise JobError("Diese Episode wartet nicht auf Antworten")
        job_dir = self.job_dir(job_id)
        saved = clarify.save_answers(job_dir, answers)
        EpisodeLog(job_dir / "log.jsonl").write(
            "answers", stage="research", answered=sum(1 for a in saved if a["answer"]), questions=len(saved))
        self.store.update(job_id, status="queued", message="Antworten erhalten", finished_at=None)
        self._wake()

    async def cancel(self, job_id: str) -> None:
        job = self._get(job_id)
        if job.status in ("queued", "waiting"):
            self.store.update(job_id, status="cancelled", finished_at=now())
        elif job.status == "running" and self.worker:
            await self.worker.cancel(job_id)

    def delete(self, job_id: str) -> None:
        job = self._get(job_id)
        if job.active:
            raise JobError("Laufende Jobs zuerst abbrechen")
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
        self.store.delete(job_id)

    def _get(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise JobError("Job nicht gefunden")
        return job

    def _wake(self) -> None:
        if self.worker:
            self.worker.wake()


class Worker:
    POLL_SECONDS = 5  # also picks up jobs enqueued by the CLI in another process

    def __init__(self, service: JobService, context_factory: ContextFactory | None = None):
        self.service = service
        self.store = service.store
        self.context_factory = context_factory or (lambda d: load_context(d, service.settings))
        self._wake_event = asyncio.Event()
        self._current: tuple[str, asyncio.Task] | None = None
        self._cancel_requested: set[str] = set()
        self.listeners: list[JobListener] = []
        service.worker = self

    async def _emit(self, job_id: str, event: str) -> None:
        job = self.store.get(job_id)
        for listener in self.listeners:
            try:
                await listener.job_event(job, event)
            except Exception:  # a broken notifier must never break the job
                log.exception("Listener %r failed on %s for %s", listener, event, job_id)

    def current_job_id(self) -> str | None:
        return self._current[0] if self._current else None

    def wake(self) -> None:
        self._wake_event.set()

    async def cancel(self, job_id: str) -> None:
        if self._current and self._current[0] == job_id:
            self._cancel_requested.add(job_id)
            self._current[1].cancel()

    async def run_forever(self) -> None:
        requeued = self.store.requeue_interrupted()
        if requeued:
            log.info("Requeued %d interrupted job(s)", requeued)
        while True:
            job = self.store.claim_next()
            if job is None:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), self.POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue
            await self.run_job(job)

    async def run_job(self, job: Job) -> None:
        job_dir = self.service.job_dir(job.id)

        async def progress(stage: str, message: str) -> None:
            self.store.update(job.id, stage=stage, message=message)
            await self._emit(job.id, "progress")

        async def set_title(title: str) -> None:
            self.store.update(job.id, title=title)
            await self._emit(job.id, "progress")

        if not job.title and (known := current_title(job_dir)):
            self.store.update(job.id, title=known)
        await self._emit(job.id, "started")
        task = None
        try:
            ctx = self.context_factory(job_dir)
            ctx.on_title = set_title
            task = asyncio.create_task(run_pipeline(ctx, progress=progress, from_stage=job.from_stage))
            self._current = (job.id, task)
            await task
        except asyncio.CancelledError:
            if job.id in self._cancel_requested:
                self._cancel_requested.discard(job.id)
                self.store.update(
                    job.id, status="cancelled", message="Abgebrochen", from_stage=None,
                    finished_at=now(), cost_usd=episode_cost(job_dir),
                )
                self._current = None
                await self._emit(job.id, "cancelled")
                return
            # Server shutdown: leave the job 'running'; it is requeued on the next start.
            if task and not task.done():
                task.cancel()
            raise
        except NeedsInput as exc:
            self.store.update(
                job.id, status="waiting", message=f"Wartet auf deine Antworten ({len(exc.questions)} Rückfragen)",
                cost_usd=episode_cost(job_dir),
            )
            self._current = None
            await self._emit(job.id, "waiting")
            return
        except Exception as exc:
            log.exception("Job %s failed", job.id)
            stage = (self.store.get(job.id) or job).stage
            self.store.update(
                job.id, status="failed", error=format_error(exc, stage),
                from_stage=None, finished_at=now(), cost_usd=episode_cost(job_dir),
            )
            self._current = None
            await self._emit(job.id, "failed")
            return
        finally:
            self._current = None

        episode = json.loads((job_dir / "episode.json").read_text(encoding="utf-8"))
        self.store.update(
            job.id, status="done", stage="", message="Fertig", from_stage=None,
            title=episode["title"], duration_s=episode["duration_seconds"],
            finished_at=now(), cost_usd=episode_cost(job_dir),
        )
        await self._emit(job.id, "done")
