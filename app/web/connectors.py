"""HTTP connectors: the private podcast feed and the REST API (with webhooks)."""

from __future__ import annotations

import json
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.feed import build_feed, chapters_json
from app.jobs import JobError, JobService
from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.notifiers import episode_payload


def base_url(settings: Settings, request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def register_feed(app: FastAPI, settings: Settings, service: JobService, token: str) -> None:
    def check(given: str) -> bool:
        return secrets.compare_digest(given.encode(), token.encode())

    def episode_file(job_id: str, name: str):
        try:
            job = service.store.get(job_id)
            path = service.job_dir(job_id) / name
        except JobError:
            return None
        return path if job and job.status == "done" and path.exists() else None

    @app.get("/feed/{given}.xml")
    async def feed(request: Request, given: str):
        if not check(given):
            return Response(status_code=404)
        xml = build_feed(settings, service.store.list(500), base_url(settings, request), token)
        return Response(xml, media_type="application/rss+xml; charset=utf-8")

    @app.get("/feed/{given}/audio/{job_id}.mp3")
    async def feed_audio(given: str, job_id: str):
        path = episode_file(job_id, "episode.mp3") if check(given) else None
        return FileResponse(path, media_type="audio/mpeg") if path else Response(status_code=404)

    @app.get("/feed/{given}/handout/{job_id}.pdf")
    async def feed_handout(given: str, job_id: str):
        path = episode_file(job_id, "handout.pdf") if check(given) else None
        return FileResponse(path, media_type="application/pdf") if path else Response(status_code=404)

    @app.get("/feed/{given}/chapters/{job_id}.json")
    async def feed_chapters(given: str, job_id: str):
        path = episode_file(job_id, "episode.json") if check(given) else None
        if not path:
            return Response(status_code=404)
        data = chapters_json(json.loads(path.read_text(encoding="utf-8")))
        return JSONResponse(data, media_type="application/json+chapters")

    @app.get("/feed/{given}/cover.jpg")
    async def feed_cover(given: str):
        path = settings.assets_dir / "cover.jpg"
        return FileResponse(path, media_type="image/jpeg") if check(given) and path.exists() else Response(status_code=404)


class ApiEpisodeRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=4000)
    length: Length = Length.mittel
    research_depth: ResearchDepth = ResearchDepth.medium
    handout: bool = False
    extra_instructions: str = ""
    callback_url: str | None = None


def register_api(app: FastAPI, settings: Settings, service: JobService) -> None:
    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, given = header.partition(" ")
        return (
            bool(settings.api_token)
            and scheme.lower() == "bearer"
            and secrets.compare_digest(given.strip().encode(), settings.api_token.encode())
        )

    def deny() -> JSONResponse:
        if not settings.api_token:
            return JSONResponse({"error": "API disabled: set API_TOKEN"}, status_code=404)
        return JSONResponse({"error": "invalid or missing bearer token"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    def get_job(job_id: str):
        try:
            job = service.store.get(job_id)
            return job, service.job_dir(job_id) if job else None
        except JobError:
            return None, None

    @app.post("/api/episodes")
    async def api_create(request: Request):
        if not authorized(request):
            return deny()
        try:
            body = ApiEpisodeRequest.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            detail = exc.errors() if isinstance(exc, ValidationError) else str(exc)
            return JSONResponse({"error": "invalid request", "detail": json.loads(json.dumps(detail, default=str))},
                                status_code=422)
        if body.callback_url and urlparse(body.callback_url).scheme not in ("http", "https"):
            return JSONResponse({"error": "callback_url must be http(s)"}, status_code=422)
        job = service.submit(
            EpisodeRequest(topic=body.topic, options=EpisodeOptions(
                length=body.length, research_depth=body.research_depth, handout=body.handout,
                extra_instructions=body.extra_instructions,
            )),
            origin="api",
        )
        if body.callback_url:
            (service.job_dir(job.id) / "webhook.json").write_text(
                json.dumps({"callback_url": body.callback_url}), encoding="utf-8"
            )
        return JSONResponse(episode_payload(service, job, base_url(settings, request)), status_code=201)

    @app.get("/api/episodes")
    async def api_list(request: Request, limit: int = 20):
        if not authorized(request):
            return deny()
        jobs = service.store.list(max(1, min(limit, 200)))
        return {"episodes": [episode_payload(service, j, base_url(settings, request)) for j in jobs]}

    @app.get("/api/episodes/{job_id}")
    async def api_get(request: Request, job_id: str):
        if not authorized(request):
            return deny()
        job, _ = get_job(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return episode_payload(service, job, base_url(settings, request))

    @app.get("/api/episodes/{job_id}/audio")
    async def api_audio(request: Request, job_id: str):
        if not authorized(request):
            return deny()
        job, job_dir = get_job(job_id)
        if job is None or not (job_dir / "episode.mp3").exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(job_dir / "episode.mp3", media_type="audio/mpeg", filename=f"{job_id}.mp3")

    @app.get("/api/episodes/{job_id}/handout")
    async def api_handout(request: Request, job_id: str):
        if not authorized(request):
            return deny()
        job, job_dir = get_job(job_id)
        if job is None or not (job_dir / "handout.pdf").exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(job_dir / "handout.pdf", media_type="application/pdf", filename=f"{job_id}.pdf")

    @app.post("/api/episodes/{job_id}/cancel")
    async def api_cancel(request: Request, job_id: str):
        if not authorized(request):
            return deny()
        try:
            await service.cancel(job_id)
        except JobError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return episode_payload(service, service.store.get(job_id), base_url(settings, request))

    @app.post("/api/episodes/{job_id}/retry")
    async def api_retry(request: Request, job_id: str, from_stage: str | None = None):
        if not authorized(request):
            return deny()
        try:
            service.retry(job_id, from_stage)
        except JobError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return episode_payload(service, service.store.get(job_id), base_url(settings, request))
