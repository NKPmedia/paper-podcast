import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.auth import verify_password
from app.config import Settings, apply_overrides
from app.web import create_app


@pytest.fixture
def fresh(settings, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(settings.data_dir / "claude-none"))
    settings = apply_overrides(Settings(_env_file=None, data_dir=settings.data_dir))
    app = create_app(settings, start_worker=False)
    return settings, app


def setup_data(app, **overrides):
    data = {
        "code": app.state.setup_code, "password": "ein-gutes-passwort", "password2": "ein-gutes-passwort",
        "claude_code_oauth_token": "sk-ant-oat01-test", "podcast_name": "Forschungsfunk",
        "host_name": "Mia", "expert_name": "Prof. Tom", "public_base_url": "https://pod.example/",
        "telegram_bot_token": "", "telegram_allowed_chat_ids": "", "gemini_api_key": "",
    }
    data.update(overrides)
    return data


def test_first_run_redirects_to_setup(fresh):
    _, app = fresh
    with TestClient(app) as client:
        for path in ("/", "/login", "/prompts", "/episodes/x"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/setup"
        assert client.get("/healthz").status_code == 200
        page = client.get("/setup")
        assert "Willkommen" in page.text and "Einrichtungscode" in page.text and "claude setup-token" in page.text
        assert "Anmeldung auch über unverschlüsseltes HTTP erlauben" in page.text  # TestClient speaks http


def test_setup_validation(fresh):
    settings, app = fresh
    with TestClient(app) as client:
        bad = client.post("/setup", data=setup_data(app, code="FALSCH", password="kurz", password2="kurz",
                                                    claude_code_oauth_token="", public_base_url="ftp://x",
                                                    telegram_allowed_chat_ids="abc", telegram_bot_token="nope"))
        assert bad.status_code == 400
        for message in ("Falscher Code", "Mindestens 10 Zeichen", "Ohne Claude-Token", "https://",
                        "Nur Zahlen", "@BotFather"):
            assert message in bad.text, message
        assert "Forschungsfunk" in bad.text  # input is kept …
        assert "sk-ant-oat01-test" not in bad.text  # … but secrets are not echoed back
        assert not settings.web_password_hash

        mismatch = client.post("/setup", data=setup_data(app, password2="ein-anderes-passwort"))
        assert "stimmen nicht überein" in mismatch.text


def test_setup_completes_and_persists(fresh):
    settings, app = fresh
    with TestClient(app) as client:
        response = client.post("/setup", data=setup_data(app, allow_http="on", cookie_secure_field="1"))
        assert response.status_code == 200 and "Fertig eingerichtet" in response.text
        assert "Forschungsfunk" in response.text  # logged in, new name in the header

        assert verify_password("ein-gutes-passwort", settings.web_password_hash)
        assert settings.podcast_name == "Forschungsfunk" and settings.host_name == "Mia"
        assert settings.public_base_url == "https://pod.example"
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/"

        stored = json.loads((settings.data_dir / "settings.json").read_text())
        assert stored["podcast_name"] == "Forschungsfunk" and stored["cookie_secure"] is False
        assert oct((settings.data_dir / "settings.json").stat().st_mode)[-3:] == "600"

    # a restart picks the values up again
    reloaded = apply_overrides(Settings(_env_file=None, data_dir=settings.data_dir))
    assert reloaded.podcast_name == "Forschungsfunk" and verify_password("ein-gutes-passwort", reloaded.web_password_hash)

    with TestClient(create_app(reloaded, start_worker=False)) as client:
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
        assert client.post("/login", data={"password": "ein-gutes-passwort", "csrf": token}).status_code == 200


def test_env_values_are_locked(settings, monkeypatch):
    monkeypatch.setenv("PODCAST_NAME", "Aus der Env")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-env")
    s = apply_overrides(Settings(_env_file=None, data_dir=settings.data_dir, cookie_secure=False))
    app = create_app(s, start_worker=False)
    with TestClient(app) as client:
        page = client.get("/setup")
        assert "festgelegt in der .env" in page.text
        client.post("/setup", data=setup_data(app, podcast_name="Überschrieben?", claude_code_oauth_token=""))
        assert s.podcast_name == "Aus der Env" and s.web_password_hash


def test_settings_page(fresh):
    settings, app = fresh
    with TestClient(app) as client:
        client.post("/setup", data=setup_data(app, gemini_api_key="AIza-geheim", allow_http="on", cookie_secure_field="1"))
        page = client.get("/settings")
        assert "gesetzt ✓" in page.text and "AIza-geheim" not in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        base = {"csrf": token, "podcast_name": "Neu", "host_name": "Mia", "expert_name": "Tom",
                "public_base_url": "", "telegram_allowed_chat_ids": "", "cookie_secure_field": "1", "allow_http": "on"}

        client.post("/settings", data=base | {"gemini_api_key": ""})
        assert settings.podcast_name == "Neu" and settings.gemini_api_key == "AIza-geheim"  # blank keeps secrets
        client.post("/settings", data=base | {"clear_gemini_api_key": "on"})
        assert settings.gemini_api_key == ""

        wrong = client.post("/settings", data=base | {"current_password": "falsch", "password": "neues-passwort-1",
                                                      "password2": "neues-passwort-1"})
        assert "aktuelle Passwort ist falsch" in wrong.text
        client.post("/settings", data=base | {"current_password": "ein-gutes-passwort", "password": "neues-passwort-1",
                                              "password2": "neues-passwort-1"})
        assert verify_password("neues-passwort-1", settings.web_password_hash)


def test_secure_cookie_flag(fresh):
    settings, app = fresh
    with TestClient(app, base_url="https://pod.example") as client:
        response = client.get("/setup")
        assert "secure" in response.headers.get("set-cookie", "").lower()
    settings.cookie_secure = False
    with TestClient(app) as client:  # plain http, plain-http logins allowed
        response = client.get("/setup")
        assert "secure" not in response.headers.get("set-cookie", "").lower()
    settings.cookie_secure = True
    with TestClient(app) as client:
        assert "secure" in client.get("/setup").headers.get("set-cookie", "").lower()
