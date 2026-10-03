import os
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import verify_password
from app.config import Settings, SettingsError, load_settings, save_settings
from app.web import create_app
from app.web.settings_ui import SECTIONS, SETUP_FIELDS


@pytest.fixture
def fresh(settings, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(settings.data_dir / "claude-none"))
    settings = load_settings(settings.data_dir)
    return settings, create_app(settings, start_worker=False)


def setup_data(app, **overrides):
    data = {
        "code": app.state.setup_code, "password": "ein-gutes-passwort", "password2": "ein-gutes-passwort",
        "claude_code_oauth_token": "sk-ant-oat01-test", "podcast_name": "Forschungsfunk",
        "host_name": "Mia", "expert_name": "Prof. Tom", "telegram_bot_token": "",
    }
    data.update(overrides)
    return data


def csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def test_settings_live_in_sqlite(tmp_path):
    s = load_settings(tmp_path)
    assert s.session_secret and s.feed_token  # generated on first start
    db = tmp_path / "app.sqlite3"
    assert oct(db.stat().st_mode)[-3:] == "600"
    save_settings(s, {"words_per_minute": 150, "cookie_secure": False, "podcast_name": "X"})
    again = load_settings(tmp_path)
    assert (again.words_per_minute, again.cookie_secure, again.podcast_name) == (150, False, "X")
    assert again.session_secret == s.session_secret
    keys = {k for (k,) in sqlite3.connect(db).execute("SELECT key FROM settings")}
    assert {"words_per_minute", "session_secret", "feed_token"} <= keys
    with pytest.raises(SettingsError):
        save_settings(s, {"words_per_minute": "viele"})
    with pytest.raises(SettingsError):
        save_settings(s, {"no_such_setting": 1})


def test_setup_dialog_asks_only_the_essentials(fresh):
    _, app = fresh
    assert [f.key for f in SETUP_FIELDS] == [
        "podcast_name", "host_name", "expert_name", "claude_code_oauth_token", "telegram_bot_token"]
    with TestClient(app) as client:
        for path in ("/", "/login", "/settings", "/episodes/x"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/setup"
        page = client.get("/setup")
        assert "Einrichtungscode" in page.text and "claude setup-token" in page.text
        assert "Gemini" not in page.text and "Bitrate" not in page.text  # those live on the settings page
        assert "Anmeldung über unverschlüsseltes HTTP erlauben" in page.text  # TestClient speaks http


def test_setup_validation(fresh):
    settings, app = fresh
    with TestClient(app) as client:
        bad = client.post("/setup", data=setup_data(app, code="FALSCH", password="kurz", password2="kurz",
                                                    claude_code_oauth_token="", telegram_bot_token="nope",
                                                    host_name=""))
        assert bad.status_code == 400
        for message in ("Falscher Code", "Mindestens 10 Zeichen", "Ohne Claude-Token", "@BotFather", "Bitte ausfüllen"):
            assert message in bad.text, message
        assert "Forschungsfunk" in bad.text and "nope" not in bad.text  # input kept, secrets not echoed
        assert not settings.web_password_hash


def test_setup_completes_and_persists(fresh):
    settings, app = fresh
    with TestClient(app) as client:
        response = client.post("/setup", data=setup_data(app, allow_http="on"))
        assert response.status_code == 200 and "Fertig eingerichtet" in response.text
        assert "Forschungsfunk" in response.text  # logged in, new name in the header
        assert verify_password("ein-gutes-passwort", settings.web_password_hash)
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/"

    reloaded = load_settings(settings.data_dir)
    assert reloaded.podcast_name == "Forschungsfunk" and reloaded.cookie_secure is False
    with TestClient(create_app(reloaded, start_worker=False)) as client:
        token = csrf(client, "/login")
        assert client.post("/login", data={"password": "ein-gutes-passwort", "csrf": token}).status_code == 200


def test_settings_page_sections(fresh):
    settings, app = fresh
    with TestClient(app) as client:
        client.post("/setup", data=setup_data(app, allow_http="on"))
        page = client.get("/settings")
        for section in SECTIONS:
            assert f'id="{section.id}"' in page.text and section.title.replace("&", "&amp;") in page.text
        token = csrf(client, "/settings")

        # voices: select + secret
        client.post("/settings/voices", data={"csrf": token, "tts_provider": "edge", "gemini_api_key": "AIza-geheim",
                                              "gemini_tts_model": "m", "gemini_voice_host": "Kore",
                                              "gemini_voice_expert": "Puck", "edge_voice_host": "a",
                                              "edge_voice_expert": "b", "edge_concurrency": "2"})
        assert (settings.tts_provider, settings.gemini_api_key, settings.gemini_voice_expert) == ("edge", "AIza-geheim", "Puck")
        page = client.get("/settings")
        assert "AIza-geheim" not in page.text and "gesetzt ✓" in page.text
        client.post("/settings/voices", data={"csrf": token, "tts_provider": "auto", "gemini_api_key": ""})
        assert settings.gemini_api_key == "AIza-geheim"  # blank keeps the secret
        client.post("/settings/voices", data={"csrf": token, "gemini_api_key__clear": "on"})
        assert settings.gemini_api_key == ""

        # audio: numbers with range checks, German decimal comma
        bad = client.post("/settings/audio", data={"csrf": token, "pause_line_ms": "viel", "target_lufs": "-50",
                                                   "pause_chapter_ms": "1000", "mp3_bitrate": "320k"})
        assert bad.status_code == 400
        assert "Bitte eine Zahl" in bad.text and "Erlaubt sind Werte" in bad.text and "Ungültige Auswahl" in bad.text
        client.post("/settings/audio", data={"csrf": token, "pause_line_ms": "500", "target_lufs": "-14,5",
                                              "pause_chapter_ms": "1500", "mp3_bitrate": "128k"})
        assert (settings.pause_line_ms, settings.target_lufs, settings.mp3_bitrate) == (500, -14.5, "128k")

        # booleans and other sections
        client.post("/settings/telegram", data={"csrf": token, "telegram_allowed_chat_ids": "1, 2",
                                                "telegram_notify_all__present": "1", "telegram_api_base_url": ""})
        assert settings.telegram_allowed_chat_ids == "1,2" and settings.telegram_notify_all is False
        bad = client.post("/settings/access", data={"csrf": token, "timezone": "Mars/Olympus", "public_base_url": "x"})
        assert "Unbekannte Zeitzone" in bad.text and "https://" in bad.text
        assert client.post("/settings/nope", data={"csrf": token}).status_code == 404

        # all of it persisted in SQLite
        stored = load_settings(settings.data_dir)
        assert (stored.tts_provider, stored.mp3_bitrate, stored.telegram_notify_all) == ("auto", "128k", False)

        # password change
        wrong = client.post("/settings/password", data={"csrf": token, "current_password": "falsch",
                                                        "password": "neues-passwort-1", "password2": "neues-passwort-1"})
        assert "aktuelle Passwort ist falsch" in wrong.text
        client.post("/settings/password", data={"csrf": token, "current_password": "ein-gutes-passwort",
                                                "password": "neues-passwort-1", "password2": "neues-passwort-1"})
        assert verify_password("neues-passwort-1", load_settings(settings.data_dir).web_password_hash)


def test_secure_cookie_flag(fresh):
    settings, app = fresh
    with TestClient(app, base_url="https://pod.example") as client:
        assert "secure" in client.get("/setup").headers.get("set-cookie", "").lower()
    settings.cookie_secure = False
    with TestClient(app) as client:
        assert "secure" not in client.get("/setup").headers.get("set-cookie", "").lower()
    settings.cookie_secure = True
    with TestClient(app) as client:
        assert "secure" in client.get("/setup").headers.get("set-cookie", "").lower()


def test_cli_set_password(tmp_path, monkeypatch):
    from app import cli, config

    monkeypatch.setattr(config, "get_settings", lambda: load_settings(tmp_path))
    monkeypatch.setattr(cli, "get_settings", lambda: load_settings(tmp_path))
    answers = iter(["cli-passwort-123", "cli-passwort-123"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert cli.main(["set-password"]) == 0
    assert verify_password("cli-passwort-123", load_settings(tmp_path).web_password_hash)
