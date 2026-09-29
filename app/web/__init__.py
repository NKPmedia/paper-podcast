"""Password-protected web UI: new episodes, episode list and player, prompt and skill editors."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError
from markdown_it import MarkdownIt
from starlette.middleware.sessions import SessionMiddleware

from app.assets import FILES as ASSET_FILES
from app.assets import MAX_UPLOAD_BYTES, AssetError, save_cover, save_jingle
from app.auth import LoginThrottle, load_or_create_secret, verify_password
from app.config import Settings, get_settings
from app.db import JobStore
from app.jobs import ContextFactory, JobError, JobService, Worker
from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.pipeline import STAGE_ARTIFACTS, prompt_context, stages_for
from app.prompts import BLOCK_DESCRIPTIONS, PromptStore
from app.skills import STAGES as SKILL_STAGES
from app.skills import MAX_ZIP_BYTES, SkillError, SkillStore
from app.web.connectors import base_url, register_feed

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
STAGE_LABELS = {"research": "Recherche", "script": "Skript", "handout": "Handout", "tts": "Sprachausgabe", "audio": "Audio"}
STATUS_LABELS = {
    "queued": "Wartet",
    "running": "Läuft",
    "done": "Fertig",
    "failed": "Fehler",
    "cancelled": "Abgebrochen",
}
LENGTH_LABELS = {"kurz": "Kurz (~5 min)", "mittel": "Mittel (~12 min)", "lang": "Lang (~25 min)"}
DEPTH_LABELS = {"quick": "Schnell", "medium": "Mittel", "deep": "Gründlich"}

_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


class NeedsLogin(Exception):
    pass


class BadCsrf(Exception):
    pass


def _read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _safe_url(url: str) -> str:
    return url if urlparse(url or "").scheme in ("http", "https") else ""


def create_app(
    settings: Settings | None = None,
    context_factory: ContextFactory | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    if not settings.web_password_hash:
        raise RuntimeError(
            "WEB_PASSWORD_HASH ist nicht gesetzt. Erzeugen mit: python -m app.cli hash-password"
        )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(settings.db_path)
    prompts = PromptStore(settings.prompts_dir)
    skills = SkillStore(settings.skills_dir, settings.data_dir / "skills.json")
    service = JobService(settings, store, prompts)
    worker = Worker(service, context_factory)
    throttle = LoginThrottle()
    feed_token = load_or_create_secret(settings.feed_token, settings.data_dir / "feed_token")

    tz = ZoneInfo(settings.timezone)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        telegram = None
        if start_worker and settings.telegram_bot_token:
            from app.telegram_bot import TelegramRunner

            telegram = TelegramRunner(settings, service, worker)
            try:
                await telegram.start()
            except Exception:  # bad token, no network: keep the web UI running
                log.exception("Telegram bot could not start")
                telegram = None
        task = asyncio.create_task(worker.run_forever()) if start_worker else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if telegram:
            await telegram.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.worker = worker
    app.add_middleware(
        SessionMiddleware,
        secret_key=load_or_create_secret(settings.session_secret, settings.data_dir / "session_secret"),
        session_cookie="paper_podcast",
        max_age=30 * 24 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    register_feed(app, settings, service, feed_token)
    templates = Jinja2Templates(directory=WEB_DIR / "templates")

    def fmt_time(value: str | None) -> str:
        if not value:
            return ""
        return datetime.fromisoformat(value).astimezone(tz).strftime("%d.%m.%Y %H:%M")

    def fmt_duration(seconds: int | None) -> str:
        if not seconds:
            return ""
        return f"{seconds // 60}:{seconds % 60:02d}"

    templates.env.filters.update(time=fmt_time, duration=fmt_duration, markdown=_markdown.render, safe_url=_safe_url)
    templates.env.globals.update(
        status_labels=STATUS_LABELS, stage_labels=STAGE_LABELS, podcast_name=settings.podcast_name
    )

    # --- helpers ------------------------------------------------------------------

    def require_user(request: Request) -> None:
        if not request.session.get("user"):
            raise NeedsLogin()

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = request.session["csrf"] = secrets.token_urlsafe(32)
        return token

    def check_csrf(request: Request, token: str) -> None:
        if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
            raise BadCsrf()

    def flash(request: Request, message: str, kind: str = "info") -> None:
        request.session.setdefault("flash", []).append({"message": message, "kind": kind})

    def render(request: Request, template: str, status_code: int = 200, **context) -> Response:
        messages = request.session.pop("flash", [])
        return templates.TemplateResponse(
            request, template, {"csrf": csrf_token(request), "messages": messages, **context},
            status_code=status_code,
        )

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(NeedsLogin)
    async def _needs_login(request: Request, exc: NeedsLogin):
        if request.url.path.endswith(".json"):
            return JSONResponse({"error": "login required"}, status_code=401)
        return redirect(f"/login?next={quote(request.url.path)}")

    @app.exception_handler(BadCsrf)
    async def _bad_csrf(request: Request, exc: BadCsrf):
        return Response("Ungültiges Formular-Token. Bitte Seite neu laden.", status_code=400)

    # --- auth ---------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/login")
    async def login_form(request: Request, next: str = "/"):
        return render(request, "login.html", next=next)

    @app.post("/login")
    async def login(request: Request, password: str = Form(...), csrf: str = Form(""), next: str = Form("/")):
        check_csrf(request, csrf)
        client = request.client.host if request.client else "unknown"
        if throttle.blocked(client):
            return render(request, "login.html", 429, next=next, error="Zu viele Fehlversuche. Bitte später erneut.")
        if not verify_password(password, settings.web_password_hash):
            throttle.fail(client)
            await asyncio.sleep(1)
            return render(request, "login.html", 401, next=next, error="Falsches Passwort.")
        throttle.reset(client)
        request.session.clear()
        request.session["user"] = "owner"
        return redirect(next if next.startswith("/") and not next.startswith("//") else "/")

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        request.session.clear()
        return redirect("/login")

    # --- episodes -----------------------------------------------------------------

    def job_summary(job) -> dict:
        return {
            "id": job.id,
            "status": job.status,
            "status_label": STATUS_LABELS[job.status],
            "stage": job.stage,
            "stage_label": STAGE_LABELS.get(job.stage, ""),
            "message": job.message,
            "error": job.error,
            "title": job.display_title,
            "duration": fmt_duration(job.duration_s),
            "active": job.active,
        }

    @app.get("/")
    async def index(request: Request):
        require_user(request)
        return render(
            request, "index.html",
            jobs=store.list(100),
            lengths=LENGTH_LABELS, depths=DEPTH_LABELS,
            blocks=BLOCK_DESCRIPTIONS, prompt_names=prompts.names(),
            skills=skills.all(),
        )

    @app.get("/jobs.json")
    async def jobs_json(request: Request):
        require_user(request)
        return {"jobs": [job_summary(j) for j in store.list(100)]}

    @app.post("/episodes")
    async def create_episode(request: Request):
        require_user(request)
        form = await request.form()
        check_csrf(request, form.get("csrf", ""))
        topic = str(form.get("topic", "")).strip()
        if not topic:
            flash(request, "Bitte ein Thema angeben.", "error")
            return redirect("/")
        block_overrides = {
            name: str(form.get(f"block_{name}", "")).strip()
            for name in prompts.names()
            if str(form.get(f"block_{name}", "")).strip()
        }
        try:
            options = EpisodeOptions(
                length=Length(form.get("length", "mittel")),
                research_depth=ResearchDepth(form.get("depth", "medium")),
                handout=form.get("handout") == "on",
                extra_instructions=str(form.get("extra", "")).strip(),
                block_overrides=block_overrides,
                extra_skills=[str(s) for s in form.getlist("extra_skills")],
            )
            job = service.submit(EpisodeRequest(topic=topic, options=options), origin="web")
        except (ValueError, KeyError, TemplateSyntaxError, UndefinedError) as exc:
            flash(request, f"Konnte die Episode nicht anlegen: {exc}", "error")
            return redirect("/")
        flash(request, "Episode wurde in die Warteschlange gestellt.")
        return redirect(f"/episodes/{job.id}")

    def load_job(job_id: str):
        job = store.get(job_id)
        if job is None:
            return None, None
        try:
            return job, service.job_dir(job_id)
        except JobError:
            return None, None

    @app.get("/episodes/{job_id}")
    async def episode(request: Request, job_id: str):
        require_user(request)
        job, job_dir = load_job(job_id)
        if job is None:
            return render(request, "error.html", 404, message="Episode nicht gefunden.")
        request_data = _read_json(job_dir / "request.json", {})
        job_stages = stages_for(request_data.get("request", {}).get("options", {}).get("handout", False))
        research_md = job_dir / "research.md"
        log_entries = [
            json.loads(line)
            for line in (job_dir / "log.jsonl").read_text(encoding="utf-8").splitlines()
        ] if (job_dir / "log.jsonl").exists() else []
        return render(
            request, "episode.html",
            job=job,
            options=request_data.get("request", {}).get("options", {}),
            lengths=LENGTH_LABELS, depths=DEPTH_LABELS,
            stages=[(s, (job_dir / STAGE_ARTIFACTS[s]).exists()) for s in job_stages],
            has_handout=(job_dir / "handout.pdf").exists(),
            episode=_read_json(job_dir / "episode.json"),
            script=_read_json(job_dir / "script.json"),
            research_html=_markdown.render(research_md.read_text(encoding="utf-8")) if research_md.exists() else "",
            sources=_read_json(job_dir / "sources.json", []),
            candidates=_read_json(job_dir / "candidates.json", []),
            selection=_read_json(job_dir / "selection.json"),
            papers=_read_json(job_dir / "papers/index.json", []),
            claude_calls=[e for e in log_entries if e.get("event") == "claude"],
            names={"host": settings.host_name, "expert": settings.expert_name},
            stage_names=job_stages,
        )

    @app.get("/episodes/{job_id}/status.json")
    async def episode_status(request: Request, job_id: str):
        require_user(request)
        job, _ = load_job(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return job_summary(job)

    @app.get("/episodes/{job_id}/audio")
    async def episode_audio(request: Request, job_id: str, download: bool = False):
        require_user(request)
        job, job_dir = load_job(job_id)
        if job is None or not (job_dir / "episode.mp3").exists():
            return Response("Nicht gefunden", status_code=404)
        filename = f"{job_id}.mp3" if download else None
        return FileResponse(job_dir / "episode.mp3", media_type="audio/mpeg", filename=filename)

    @app.get("/episodes/{job_id}/handout")
    async def episode_handout(request: Request, job_id: str, download: bool = False):
        require_user(request)
        job, job_dir = load_job(job_id)
        if job is None or not (job_dir / "handout.pdf").exists():
            return Response("Nicht gefunden", status_code=404)
        return FileResponse(job_dir / "handout.pdf", media_type="application/pdf",
                            filename=f"{job_id}-handout.pdf" if download else None)

    async def job_action(request: Request, job_id: str, action):
        require_user(request)
        form = await request.form()
        check_csrf(request, form.get("csrf", ""))
        try:
            result = action(form)
            if asyncio.iscoroutine(result):
                await result
        except JobError as exc:
            flash(request, str(exc), "error")
        return form

    @app.post("/episodes/{job_id}/cancel")
    async def cancel(request: Request, job_id: str):
        await job_action(request, job_id, lambda form: service.cancel(job_id))
        return redirect(f"/episodes/{job_id}")

    @app.post("/episodes/{job_id}/retry")
    async def retry(request: Request, job_id: str):
        await job_action(
            request, job_id, lambda form: service.retry(job_id, form.get("from_stage") or None)
        )
        return redirect(f"/episodes/{job_id}")

    @app.post("/episodes/{job_id}/delete")
    async def delete(request: Request, job_id: str):
        await job_action(request, job_id, lambda form: service.delete(job_id))
        return redirect("/" if store.get(job_id) is None else f"/episodes/{job_id}")

    # --- connections ------------------------------------------------------------------

    @app.get("/connections")
    async def connections(request: Request):
        require_user(request)
        base = base_url(settings, request)
        return render(
            request, "connections.html",
            feed_url=f"{base}/feed/{feed_token}.xml", base=base,
            public_base_url=settings.public_base_url,
            telegram_enabled=bool(settings.telegram_bot_token),
            telegram_chats=sorted(settings.telegram_chat_ids),
            assets={kind: (settings.assets_dir / name).exists() for kind, name in ASSET_FILES.items()},
            tts=tts_description(),
        )

    def tts_description() -> str:
        uses_gemini = settings.tts_provider == "gemini" or (settings.tts_provider == "auto" and settings.gemini_api_key)
        if uses_gemini and settings.gemini_api_key:
            return (f"Gemini ({settings.gemini_tts_model}, Stimmen {settings.gemini_voice_host} / "
                    f"{settings.gemini_voice_expert}), bei erschöpftem Kontingent automatisch Edge")
        return f"Edge TTS (Stimmen {settings.edge_voice_host} / {settings.edge_voice_expert})"

    @app.post("/assets/{kind}")
    async def upload_asset(request: Request, kind: str, file: UploadFile, csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        if kind not in ASSET_FILES:
            return Response(status_code=404)
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        target = settings.assets_dir / ASSET_FILES[kind]
        try:
            if len(data) > MAX_UPLOAD_BYTES:
                raise AssetError("Datei zu groß (max. 20 MB)")
            if kind == "cover":
                await asyncio.to_thread(save_cover, data, target)
                flash(request, "Cover gespeichert. Es erscheint im Feed und in neuen Episoden.")
            else:
                seconds = await asyncio.to_thread(save_jingle, data, target)
                flash(request, f"{kind.capitalize()} gespeichert ({seconds:.1f} s). Gilt für neue Episoden.")
        except AssetError as exc:
            flash(request, str(exc), "error")
        return redirect("/connections#klang")

    @app.post("/assets/{kind}/delete")
    async def delete_asset(request: Request, kind: str, csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        if kind in ASSET_FILES:
            (settings.assets_dir / ASSET_FILES[kind]).unlink(missing_ok=True)
            flash(request, "Entfernt.")
        return redirect("/connections#klang")

    @app.get("/assets/{kind}")
    async def get_asset(request: Request, kind: str):
        require_user(request)
        path = settings.assets_dir / ASSET_FILES.get(kind, "-")
        if kind not in ASSET_FILES or not path.exists():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/jpeg" if kind == "cover" else "audio/mpeg")

    # --- prompts ------------------------------------------------------------------

    def sample_context() -> dict:
        return prompt_context(settings, EpisodeRequest(topic="Beispiel"))

    @app.get("/prompts")
    async def prompt_list(request: Request):
        require_user(request)
        items = [(n, BLOCK_DESCRIPTIONS.get(n, ""), prompts.is_overridden(n)) for n in prompts.names()]
        return render(request, "prompts.html", items=items, variables=sorted(sample_context()))

    @app.get("/prompts/{name}")
    async def prompt_edit(request: Request, name: str):
        require_user(request)
        if name not in prompts.names():
            return render(request, "error.html", 404, message="Unbekannter Prompt-Block.")
        return render(
            request, "prompt_edit.html", name=name, description=BLOCK_DESCRIPTIONS.get(name, ""),
            text=prompts.raw(name), default=prompts.default(name), overridden=prompts.is_overridden(name),
            variables=sample_context(), preview=None, error=None,
        )

    @app.post("/prompts/{name}")
    async def prompt_save(request: Request, name: str, text: str = Form(""), action: str = Form("save"),
                          csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        if name not in prompts.names():
            return render(request, "error.html", 404, message="Unbekannter Prompt-Block.")
        if action == "reset":
            prompts.reset(name)
            flash(request, f"„{name}“ auf den Standard zurückgesetzt.")
            return redirect(f"/prompts/{name}")
        text = text.replace("\r\n", "\n")
        ctx = sample_context()
        try:
            preview = Environment(undefined=StrictUndefined).from_string(text).render(**ctx)
            error = None
        except (TemplateSyntaxError, UndefinedError) as exc:
            preview, error = None, f"Vorlagenfehler: {exc}"
        if action == "save" and error is None:
            prompts.save(name, text)
            flash(request, f"„{name}“ gespeichert.")
            return redirect(f"/prompts/{name}")
        return render(
            request, "prompt_edit.html", 400 if error else 200, name=name,
            description=BLOCK_DESCRIPTIONS.get(name, ""), text=text, default=prompts.default(name),
            overridden=prompts.is_overridden(name), variables=ctx, preview=preview, error=error,
        )

    # --- skills -------------------------------------------------------------------

    @app.get("/skills")
    async def skill_list(request: Request):
        require_user(request)
        return render(
            request, "skills.html", skills=skills.all(), stage_config=skills.stage_config(),
            stages=SKILL_STAGES, skill_stage_labels={"research": "Recherche", "script": "Skript", "handout": "Handout"},
        )

    @app.post("/skills/stages")
    async def skill_stages(request: Request):
        require_user(request)
        form = await request.form()
        check_csrf(request, form.get("csrf", ""))
        config = {stage: [str(n) for n in form.getlist(f"stage_{stage}")] for stage in SKILL_STAGES}
        try:
            skills.set_stage_config(config)
            flash(request, "Skill-Zuordnung gespeichert.")
        except SkillError as exc:
            flash(request, str(exc), "error")
        return redirect("/skills")

    @app.post("/skills/new")
    async def skill_new(request: Request, name: str = Form(...), description: str = Form(""), csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        try:
            skills.create(name.strip(), description)
        except SkillError as exc:
            flash(request, str(exc), "error")
            return redirect("/skills")
        return redirect(f"/skills/{name.strip()}")

    @app.post("/skills/upload")
    async def skill_upload(request: Request, file: UploadFile, csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        try:
            name = skills.import_zip(await file.read(MAX_ZIP_BYTES + 1))
        except SkillError as exc:
            flash(request, str(exc), "error")
            return redirect("/skills")
        flash(request, f"Skill „{name}“ importiert. Aktiviere ihn für eine Stufe.")
        return redirect(f"/skills/{name}")

    @app.get("/skills/{name}")
    async def skill_detail(request: Request, name: str, file: str = "SKILL.md"):
        require_user(request)
        skill = skills.all().get(name)
        if skill is None:
            return render(request, "error.html", 404, message="Unbekannter Skill.")
        try:
            content = skills.read_file(name, file)
        except (SkillError, OSError, UnicodeDecodeError):
            flash(request, "Datei kann nicht angezeigt werden.", "error")
            return redirect(f"/skills/{name}")
        editable = skill.path.parent == skills.user_dir
        return render(request, "skill_edit.html", skill=skill, files=skills.files(name), file=file,
                      content=content, editable=editable)

    @app.post("/skills/{name}/file")
    async def skill_file_save(request: Request, name: str, file: str = Form(...), content: str = Form(""),
                              csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        try:
            skills.write_file(name, file.strip(), content.replace("\r\n", "\n"))
            flash(request, f"{file} gespeichert.")
        except (SkillError, KeyError) as exc:
            flash(request, str(exc), "error")
        return redirect(f"/skills/{name}?file={quote(file.strip())}")

    @app.post("/skills/{name}/override")
    async def skill_override(request: Request, name: str, csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        try:
            skills.override(name)
            flash(request, "Eigene Kopie angelegt; sie ersetzt ab jetzt die mitgelieferte Version.")
        except SkillError as exc:
            flash(request, str(exc), "error")
        return redirect(f"/skills/{name}")

    @app.post("/skills/{name}/remove")
    async def skill_remove(request: Request, name: str, csrf: str = Form("")):
        require_user(request)
        check_csrf(request, csrf)
        try:
            skills.remove_user_copy(name)
            flash(request, f"Eigene Version von „{name}“ entfernt.")
        except SkillError as exc:
            flash(request, str(exc), "error")
        return redirect(f"/skills/{name}" if name in skills.all() else "/skills")

    return app
