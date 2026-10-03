import asyncio
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.db import JobStore
from app.pipeline import load_context
from app.web import create_app
from tests.conftest import FakeClaude, FakeTTS, default_responses, mock_downloads

PASSWORD = "richtig-langes-passwort"


@pytest.fixture
def web_settings(settings):
    return settings.model_copy(update={"web_password_hash": hash_password(PASSWORD), "cookie_secure": False})


def make_client(web_settings, start_worker=False, responses=None):
    def factory(job_dir):
        ctx = load_context(job_dir, web_settings, claude=FakeClaude(responses or default_responses()), tts=FakeTTS())
        ctx.download_options = mock_downloads()
        return ctx

    return TestClient(create_app(web_settings, context_factory=factory, start_worker=start_worker))


def csrf(client, url="/"):
    page = client.get(url)
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def login(client):
    client.post("/login", data={"password": PASSWORD, "csrf": csrf(client, "/login"), "next": "/"})


def test_password_hashing():
    stored = hash_password("geheim")
    assert verify_password("geheim", stored) and not verify_password("falsch", stored)
    assert not verify_password("geheim", "garbage")


def test_requires_login_and_throttles(web_settings):
    with make_client(web_settings) as client:
        response = client.get("/prompts", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login?next=/prompts"
        assert client.get("/jobs.json").status_code == 401
        assert client.get("/healthz").json() == {"ok": True}

        token = csrf(client, "/login")
        assert client.post("/login", data={"password": PASSWORD, "csrf": "wrong"}).status_code == 400
        for _ in range(5):
            assert client.post("/login", data={"password": "nope", "csrf": token}).status_code == 401
        assert client.post("/login", data={"password": PASSWORD, "csrf": token}).status_code == 429


def test_login_rejects_open_redirect(web_settings):
    with make_client(web_settings) as client:
        response = client.post(
            "/login", data={"password": PASSWORD, "csrf": csrf(client, "/login"), "next": "//evil.example"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/"


def test_create_episode_runs_through_worker(web_settings):
    with make_client(web_settings, start_worker=True) as client:
        login(client)
        response = client.post("/episodes", data={
            "csrf": csrf(client), "topic": "Festkörperbatterien", "length": "kurz", "depth": "quick",
            "extra": "Fokus Autos", "block_style": "Mehr Humor.",
        })
        assert response.status_code == 200 and "Festkörperbatterien" in response.text
        job_id = response.url.path.rsplit("/", 1)[1]

        for _ in range(200):
            status = client.get(f"/episodes/{job_id}/status.json").json()
            if not status["active"]:
                break
            asyncio.run(asyncio.sleep(0.05))
        assert status["status"] == "done", status

        page = client.get(f"/episodes/{job_id}")
        assert "Testepisode" in page.text and "<audio" in page.text and "Kapitel 1" in page.text
        assert "Material für den Podcast" in page.text  # rendered research notes
        audio = client.get(f"/episodes/{job_id}/audio", headers={"Range": "bytes=0-99"})
        assert audio.status_code == 206 and len(audio.content) == 100

        store = JobStore(web_settings.db_path)
        job = store.get(job_id)
        assert job.title == "Testepisode" and job.cost_usd > 0 and job.duration_s > 0
        assert "Mehr Humor." in (web_settings.episodes_dir / job_id / "request.json").read_text()

        # re-run from the audio stage, then delete
        client.post(f"/episodes/{job_id}/retry", data={"csrf": csrf(client), "from_stage": "audio"})
        for _ in range(200):
            if not client.get(f"/episodes/{job_id}/status.json").json()["active"]:
                break
            asyncio.run(asyncio.sleep(0.05))
        assert store.get(job_id).status == "done"
        client.post(f"/episodes/{job_id}/delete", data={"csrf": csrf(client)})
        assert store.get(job_id) is None
        assert not (web_settings.episodes_dir / job_id).exists()


def test_failed_job_shows_error_and_can_resume(web_settings):
    responses = default_responses()
    responses["ScoutResult"] = [lambda call: (_ for _ in ()).throw(RuntimeError("kaputt"))] * 1
    with make_client(web_settings, start_worker=False, responses=responses) as client:
        login(client)
        client.post("/episodes", data={"csrf": csrf(client), "topic": "X", "length": "kurz", "depth": "quick"})
        app = client.app
        job = app.state.service.store.claim_next()
        asyncio.run(app.state.worker.run_job(job))
        stored = app.state.service.store.get(job.id)
        assert stored.status == "failed" and stored.error.startswith("Die Recherche ist fehlgeschlagen (der Scout). kaputt")
        assert "Traceback (most recent call last)" in stored.error and "Scout 'overview'" in stored.error
        page = client.get(f"/episodes/{job.id}")
        assert "Die Recherche ist fehlgeschlagen" in page.text and "Fortsetzen" in page.text
        assert "Technische Details (Fehlertext und Stacktrace)" in page.text and "Traceback" in page.text
        client.post(f"/episodes/{job.id}/retry", data={"csrf": csrf(client)})
        assert app.state.service.store.get(job.id).status == "queued"


def test_markdown_is_sanitized(web_settings):
    from app.web import _markdown, _safe_url

    html = _markdown.render("<script>alert(1)</script> [x](javascript:alert(1))")
    assert "<script>" not in html and "href=\"javascript" not in html
    assert _safe_url("javascript:alert(1)") == "" and _safe_url("https://a.org") == "https://a.org"


def test_prompt_editor(web_settings):
    with make_client(web_settings) as client:
        login(client)
        assert "personas" in client.get("/prompts").text
        token = csrf(client, "/prompts/style")
        bad = client.post("/prompts/style", data={"csrf": token, "text": "{{ nope }}", "action": "save"})
        assert bad.status_code == 400 and "Vorlagenfehler" in bad.text
        preview = client.post("/prompts/style", data={"csrf": token, "text": "Hallo {{ host_name }}", "action": "preview"})
        assert "Hallo Lena" in preview.text
        client.post("/prompts/style", data={"csrf": token, "text": "Sehr locker.", "action": "save"})
        assert (web_settings.prompts_dir / "style.md").read_text() == "Sehr locker."
        client.post("/prompts/style", data={"csrf": token, "action": "reset"})
        assert not (web_settings.prompts_dir / "style.md").exists()


def test_skills_pages(web_settings):
    with make_client(web_settings) as client:
        login(client)
        token = csrf(client, "/skills")
        assert "paper-research" in client.get("/skills").text

        # bundled skills are read-only until overridden
        page = client.get("/skills/fact-check")
        assert "eigene Kopie anlegen" in page.text
        client.post("/skills/fact-check/override", data={"csrf": token})
        client.post("/skills/fact-check/file", data={
            "csrf": token, "file": "SKILL.md", "content": "---\nname: fact-check\ndescription: Mine\n---\nText",
        })
        assert (web_settings.skills_dir / "fact-check" / "SKILL.md").read_text().endswith("Text")
        bad = client.post("/skills/fact-check/file", data={"csrf": token, "file": "../../x.md", "content": "x"})
        assert "Ungültiger Dateipfad" in bad.text
        client.post("/skills/fact-check/remove", data={"csrf": token})
        assert not (web_settings.skills_dir / "fact-check").exists()

        # new skill + stage assignment
        client.post("/skills/new", data={"csrf": token, "name": "mein-skill", "description": "Test"})
        assert (web_settings.skills_dir / "mein-skill" / "SKILL.md").exists()
        client.post("/skills/stages", data={"csrf": token, "stage_research": ["paper-research", "mein-skill"],
                                             "stage_script": ["fact-check"]})
        store = client.app.state.service  # noqa: F841
        from app.skills import SkillStore
        skills = SkillStore(web_settings.skills_dir, web_settings.data_dir / "skills.json")
        assert skills.stage_config() == {"research": ["paper-research", "mein-skill"], "script": ["fact-check"], "handout": []}

        # zip import, including a path traversal attempt that must be rejected
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("importiert/SKILL.md", "---\nname: importiert\ndescription: Z\n---\nX")
            zf.writestr("importiert/reference/a.md", "A")
        client.post("/skills/upload", data={"csrf": token}, files={"file": ("s.zip", buffer.getvalue())})
        assert (web_settings.skills_dir / "importiert" / "reference" / "a.md").read_text() == "A"

        evil = io.BytesIO()
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("SKILL.md", "---\nname: evil\ndescription: Z\n---\nX")
            zf.writestr("../../escape.md", "boom")
        response = client.post("/skills/upload", data={"csrf": token}, files={"file": ("e.zip", evil.getvalue())})
        assert "Ungültiger Dateipfad" in response.text
        assert not (web_settings.data_dir / "escape.md").exists()
