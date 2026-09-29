"""Job listeners that notify the outside world: email and webhooks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import httpx

from app.config import Settings
from app.db import Job
from app.jobs import JobService

log = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class Background:
    """Run deliveries as background tasks so slow receivers never block the worker."""

    def __init__(self):
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Notification failed", exc_info=task.exception())

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def episode_payload(service: JobService, job: Job, base_url: str) -> dict:
    """JSON description of a job, shared by the REST API and webhooks."""
    job_dir = service.job_dir(job.id)
    base = base_url.rstrip("/")
    data = {
        "id": job.id,
        "topic": job.topic,
        "title": job.title or None,
        "status": job.status,
        "stage": job.stage or None,
        "message": job.message or None,
        "error": job.error or None,
        "origin": job.origin,
        "duration_seconds": job.duration_s,
        "cost_usd": job.cost_usd,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "web_url": f"{base}/episodes/{job.id}",
        "audio_url": f"{base}/api/episodes/{job.id}/audio" if (job_dir / "episode.mp3").exists() else None,
        "handout_url": f"{base}/api/episodes/{job.id}/handout" if (job_dir / "handout.pdf").exists() else None,
    }
    episode_file = job_dir / "episode.json"
    if episode_file.exists() and job.status == "done":
        episode = json.loads(episode_file.read_text(encoding="utf-8"))
        data.update(summary=episode["summary"], chapters=episode["chapters"], sources=episode["sources"])
    return data


class WebhookNotifier:
    """POSTs the episode JSON to the ``callback_url`` given when the job was created via the API.

    Requests are signed: ``X-Paper-Podcast-Signature: sha256=<HMAC of the body with API_TOKEN>``.
    """

    EVENTS = {"done", "failed", "cancelled"}

    def __init__(self, settings: Settings, service: JobService, client: httpx.AsyncClient | None = None,
                 retry_delays: tuple[float, ...] = (2, 10, 60)):
        self.settings = settings
        self.service = service
        self.client = client
        self.retry_delays = retry_delays
        self.background = Background()

    def callback_url(self, job_id: str) -> str | None:
        path = self.service.job_dir(job_id) / "webhook.json"
        return json.loads(path.read_text(encoding="utf-8"))["callback_url"] if path.exists() else None

    async def job_event(self, job: Job, event: str) -> None:
        if event not in self.EVENTS or job is None:
            return
        url = self.callback_url(job.id)
        if url:
            self.background.spawn(self.deliver(job, event, url))

    async def deliver(self, job: Job, event: str, url: str) -> None:
        body = json.dumps({"event": event, "episode": episode_payload(self.service, job, self.settings.public_base_url)},
                          ensure_ascii=False).encode()
        signature = hmac.new(self.settings.api_token.encode(), body, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Paper-Podcast-Event": event,
                   "X-Paper-Podcast-Signature": f"sha256={signature}"}
        client = self.client or httpx.AsyncClient(timeout=20)
        try:
            for attempt, delay in enumerate((0, *self.retry_delays)):
                await asyncio.sleep(delay)
                try:
                    response = await client.post(url, content=body, headers=headers)
                    if response.status_code < 400:
                        return
                    log.warning("Webhook %s answered %s (attempt %d)", url, response.status_code, attempt + 1)
                except httpx.HTTPError as exc:
                    log.warning("Webhook %s failed: %s (attempt %d)", url, exc, attempt + 1)
            log.error("Giving up on webhook %s for %s", url, job.id)
        finally:
            if client is not self.client:
                await client.aclose()


class EmailNotifier:
    """Sends an email when an episode is done (with handout, optionally audio) or failed."""

    def __init__(self, settings: Settings, service: JobService):
        self.settings = settings
        self.service = service
        self.background = Background()

    @property
    def recipients(self) -> list[str]:
        return [a.strip() for a in self.settings.email_to.split(",") if a.strip()]

    async def job_event(self, job: Job, event: str) -> None:
        if job is None or event not in {e.strip() for e in self.settings.email_events.split(",")}:
            return
        message = self.build_message(job, event)
        self.background.spawn(asyncio.to_thread(self.send, message))

    def build_message(self, job: Job, event: str) -> EmailMessage:
        s = self.settings
        payload = episode_payload(self.service, job, s.public_base_url)
        message = EmailMessage()
        message["From"] = s.smtp_from or s.smtp_username
        message["To"] = ", ".join(self.recipients)
        link = f"\n\nIm Browser: {payload['web_url']}" if s.public_base_url else ""
        if event == "done":
            message["Subject"] = f"🎙️ Neue Episode: {job.display_title}"
            chapters = "\n".join(
                f"  {c['start_ms'] // 60000}:{c['start_ms'] // 1000 % 60:02d}  {c['title']}" for c in payload["chapters"]
            )
            sources = "\n".join(f"  • {x['title']} {x.get('url', '')}".rstrip() for x in payload["sources"])
            message.set_content(
                f"{job.display_title}\n\n{payload['summary']}\n\nKapitel:\n{chapters}\n\nQuellen:\n{sources}{link}\n"
            )
            job_dir = self.service.job_dir(job.id)
            attachments = [job_dir / "handout.pdf"]
            if s.email_attach_audio:
                attachments.append(job_dir / "episode.mp3")
            for path in attachments:
                self._attach(message, path, f"{job.display_title[:60]}{path.suffix}")
        else:
            label = "fehlgeschlagen" if event == "failed" else "abgebrochen"
            message["Subject"] = f"Episode {label}: {job.display_title}"
            message.set_content(f"Die Episode „{job.display_title}“ ist {label}.\n\n{job.error or ''}{link}\n")
        return message

    @staticmethod
    def _attach(message: EmailMessage, path: Path, filename: str) -> None:
        if not path.exists() or path.stat().st_size > MAX_ATTACHMENT_BYTES:
            return
        maintype, subtype = (mimetypes.guess_type(path.name)[0] or "application/octet-stream").split("/")
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=filename)

    def send(self, message: EmailMessage) -> None:
        s = self.settings
        context = ssl.create_default_context()
        if s.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        with server:
            if s.smtp_security == "starttls":
                server.starttls(context=context)
            if s.smtp_username:
                server.login(s.smtp_username, s.smtp_password)
            server.send_message(message)
