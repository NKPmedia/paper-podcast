"""The private podcast feed for podcast apps."""

from __future__ import annotations

import json
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.config import Settings
from app.feed import build_feed, chapters_json
from app.jobs import JobError, JobService


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
