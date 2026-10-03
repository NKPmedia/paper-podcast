import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.checks import KeyChecker
from app.claude import ClaudeResult, claude_error
from app.config import load_settings
from app.web import create_app


class FakeRunner:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def run(self, call):
        self.calls.append(call)
        if self.error:
            raise self.error
        return ClaudeResult(structured=None, text="OK", session_id="s", cost_usd=0.0, num_turns=1, skills_used=[])


def google_and_telegram(gemini_status=200, gemini_body="{}", telegram_ok=True):
    def handler(request: httpx.Request):
        if "generativelanguage" in request.url.host:
            assert request.headers["x-goog-api-key"] == "AIza-key"
            return httpx.Response(gemini_status, text=gemini_body)
        if "getMe" in request.url.path:
            if not telegram_ok:
                return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
            return httpx.Response(200, json={"ok": True, "result": {"username": "PodBot"}})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def ok_edge(settings):
    return None


async def broken_edge(settings):
    raise ConnectionError("blocked")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    s = load_settings(tmp_path)
    s.gemini_api_key = "AIza-key"
    s.telegram_bot_token = "123:" + "a" * 30
    s.telegram_allowed_chat_ids = "42"
    return s


async def test_all_good(configured):
    runner = FakeRunner()
    checker = KeyChecker(configured, claude=runner, http=google_and_telegram(), edge_probe=ok_edge)
    results = await checker.run()
    assert {k: v["state"] for k, v in results.items()} == {"claude": "ok", "gemini": "ok", "telegram": "ok", "edge": "ok"}
    assert "@PodBot" in results["telegram"]["message"]
    (call,) = runner.calls
    assert call.model == "haiku" and call.tools == [] and call.max_turns == 1

    # cached: no second Claude call until a relevant setting changes
    await checker.run()
    assert len(runner.calls) == 1
    configured.research_scout_model = "sonnet"
    await checker.run()
    assert len(runner.calls) == 2
    await checker.run(force=True)
    assert len(runner.calls) == 3


async def test_problems_are_reported(configured):
    runner = FakeRunner(claude_error("ProcessError", ["Invalid API key · Please run /login"]))
    checker = KeyChecker(configured, claude=runner, edge_probe=broken_edge,
                         http=google_and_telegram(400, '{"error": {"details": [{"reason": "API_KEY_INVALID"}]}}',
                                                  telegram_ok=False))
    results = await checker.run()
    assert results["claude"]["state"] == "error" and "nicht anmelden" in results["claude"]["message"]
    assert results["gemini"]["state"] == "error" and "ungültig" in results["gemini"]["message"]
    assert results["telegram"]["state"] == "error" and "BotFather" in results["telegram"]["message"]
    # Edge is only the fallback while a Gemini key is set
    assert results["edge"]["state"] == "warn"


@pytest.mark.parametrize("status,body,state,text", [
    (404, "", "error", "gibt es nicht"),
    (429, "", "warn", "Kontingent"),
    (403, "", "error", "keinen Zugriff"),
])
async def test_gemini_cases(configured, status, body, state, text):
    checker = KeyChecker(configured, claude=FakeRunner(), http=google_and_telegram(status, body), edge_probe=ok_edge)
    result = (await checker.run())["gemini"]
    assert result["state"] == state and text in result["message"]


async def test_missing_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))
    s = load_settings(tmp_path)
    runner = FakeRunner()
    results = await KeyChecker(s, claude=runner, http=google_and_telegram(), edge_probe=ok_edge).run()
    assert results["claude"]["state"] == "error" and "Kein Claude-Token" in results["claude"]["message"]
    assert results["gemini"]["state"] == "off" and results["telegram"]["state"] == "off"
    assert runner.calls == []


def test_settings_page_shows_checks(configured):
    checker = KeyChecker(configured, claude=FakeRunner(claude_error("x", ["401 Unauthorized"])),
                         http=google_and_telegram(), edge_probe=ok_edge)
    from app.auth import hash_password

    configured.web_password_hash = hash_password("ein-gutes-passwort")
    configured.cookie_secure = False
    app = create_app(configured, start_worker=False, key_checker=checker)
    with TestClient(app) as client:
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
        client.post("/login", data={"password": "ein-gutes-passwort", "csrf": token})
        page = client.get("/settings")
        assert page.text.count("Wird geprüft") == 4  # nothing cached yet: the page loads the checks itself
        data = client.get("/settings/checks.json").json()["checks"]
        assert data["claude"]["state"] == "error" and data["telegram"]["state"] == "ok"
        page = client.get("/settings")
        assert "Claude konnte sich nicht anmelden" in page.text  # cached results are rendered directly
        index = client.get("/")
        assert "Probleme mit den Einstellungen" in index.text
