import asyncio
import hashlib
import hmac
import json
import re
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.notifiers import EmailNotifier, WebhookNotifier
from app.pipeline import load_context
from app.web import create_app
from tests.conftest import FakeClaude, FakeTTS, default_responses, mock_downloads

TOKEN = "api-token-" + "x" * 30
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "podcast": "https://podcastindex.org/namespace/1.0",
      "content": "http://purl.org/rss/1.0/modules/content/"}


def make_app(settings, **overrides):
    settings = settings.model_copy(update={
        "web_password_hash": hash_password("pw-" + "y" * 12), "cookie_secure": False,
        "public_base_url": "https://pod.example", **overrides,
    })

    def factory(job_dir):
        ctx = load_context(job_dir, settings, claude=FakeClaude(default_responses()), tts=FakeTTS())
        ctx.download_options = mock_downloads()
        return ctx

    return settings, create_app(settings, context_factory=factory, start_worker=False)


def run_all(app):
    """Run queued jobs and wait for notifications, all in one event loop (like the server)."""
    service, worker = app.state.service, app.state.worker

    async def go():
        while (job := service.store.claim_next()) is not None:
            await worker.run_job(job)
        for listener in worker.listeners:
            if hasattr(listener, "background"):
                await listener.background.drain()

    asyncio.run(go())


def api(client, method, url, **kw):
    return client.request(method, url, headers={"Authorization": f"Bearer {TOKEN}"}, **kw)


def test_feed(settings):
    settings, app = make_app(settings, api_token=TOKEN)
    token = (settings.data_dir / "feed_token").read_text()
    with TestClient(app) as client:
        created = api(client, "POST", "/api/episodes", json={"topic": "Feed", "length": "kurz", "research_depth": "quick"})
        job_id = created.json()["id"]
        run_all(app)

        assert client.get("/feed/wrong.xml").status_code == 404
        response = client.get(f"/feed/{token}.xml")
        assert response.headers["content-type"].startswith("application/rss+xml")
        channel = ET.fromstring(response.content).find("channel")
        assert channel.findtext("title") == "Paper Podcast" and channel.findtext("language") == "de"
        (item,) = channel.findall("item")
        assert item.findtext("title") == "Testepisode" and item.findtext("guid") == job_id
        enclosure = item.find("enclosure")
        assert enclosure.get("url") == f"https://pod.example/feed/{token}/audio/{job_id}.mp3"
        assert int(enclosure.get("length")) == (settings.episodes_dir / job_id / "episode.mp3").stat().st_size
        assert re.fullmatch(r"00:00:\d\d", item.findtext("itunes:duration", namespaces=NS))
        assert "Kapitel 1" in item.findtext("content:encoded", namespaces=NS)
        chapters_url = item.find("podcast:chapters", NS).get("url")

        audio = client.get(f"/feed/{token}/audio/{job_id}.mp3", headers={"Range": "bytes=0-9"})
        assert audio.status_code == 206 and len(audio.content) == 10
        assert client.get(f"/feed/nope/audio/{job_id}.mp3").status_code == 404
        chapters = client.get(chapters_url.replace("https://pod.example", "")).json()
        assert chapters["version"] == "1.2.0" and chapters["chapters"][0] == {"startTime": 0, "title": "Kapitel 1"}
        assert client.get(f"/feed/{token}/handout/{job_id}.pdf").status_code == 404


def test_api_and_webhook(settings):
    settings, app = make_app(settings, api_token=TOKEN)
    received = []

    def handler(request: httpx.Request):
        received.append(request)
        return httpx.Response(500 if len(received) == 1 else 200)  # first delivery fails, retry succeeds

    webhooks = next(l for l in app.state.worker.listeners if isinstance(l, WebhookNotifier))
    webhooks.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    webhooks.retry_delays = (0,)

    with TestClient(app) as client:
        assert client.get("/api/episodes").status_code == 401
        assert client.get("/api/episodes", headers={"Authorization": "Bearer wrong"}).status_code == 401
        bad = api(client, "POST", "/api/episodes", json={"topic": ""})
        assert bad.status_code == 422
        assert api(client, "POST", "/api/episodes", json={"topic": "x", "callback_url": "file:///etc"}).status_code == 422

        created = api(client, "POST", "/api/episodes", json={
            "topic": "Per API", "length": "kurz", "research_depth": "quick", "handout": True,
            "callback_url": "https://hooks.example/done",
        })
        assert created.status_code == 201
        episode = created.json()
        assert episode["status"] == "queued" and episode["origin"] == "api"
        assert episode["web_url"] == f"https://pod.example/episodes/{episode['id']}"

        run_all(app)
        detail = api(client, "GET", f"/api/episodes/{episode['id']}").json()
        assert detail["status"] == "done" and detail["title"] == "Testepisode"
        assert detail["chapters"][0]["title"] == "Kapitel 1" and detail["handout_url"].endswith("/handout")
        assert api(client, "GET", f"/api/episodes/{episode['id']}/audio").headers["content-type"] == "audio/mpeg"
        assert api(client, "GET", f"/api/episodes/{episode['id']}/handout").headers["content-type"] == "application/pdf"
        assert len(api(client, "GET", "/api/episodes").json()["episodes"]) == 1
        assert api(client, "POST", f"/api/episodes/{episode['id']}/retry?from_stage=nope").status_code == 409

    assert len(received) == 2  # one retry after the 500
    request = received[-1]
    assert str(request.url) == "https://hooks.example/done"
    assert request.headers["X-Paper-Podcast-Event"] == "done"
    expected = hmac.new(TOKEN.encode(), request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Paper-Podcast-Signature"] == f"sha256={expected}"
    assert json.loads(request.content)["episode"]["status"] == "done"


def test_api_disabled_without_token(settings):
    _, app = make_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/episodes", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 404 and "API_TOKEN" in response.json()["error"]


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        FakeSMTP.sent.append((self, message))


def test_email_notifications(settings, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    FakeSMTP.sent = []
    settings, app = make_app(settings, api_token=TOKEN, smtp_host="smtp.example", smtp_username="bot@example",
                             smtp_password="secret", email_to="me@example, you@example")
    assert any(isinstance(l, EmailNotifier) for l in app.state.worker.listeners)
    with TestClient(app) as client:
        api(client, "POST", "/api/episodes", json={"topic": "Mail", "length": "kurz", "research_depth": "quick",
                                                    "handout": True})
        run_all(app)

        (server, message), = FakeSMTP.sent
        assert server.started_tls and server.logged_in == ("bot@example", "secret")
        assert message["Subject"] == "🎙️ Neue Episode: Testepisode" and message["To"] == "me@example, you@example"
        body = message.get_body(("plain",)).get_content()
        assert "Kapitel 1" in body and "https://pod.example/episodes/" in body
        attachments = [part.get_filename() for part in message.iter_attachments()]
        assert attachments == ["Testepisode.pdf"]

        # the connections page offers a test email
        client.post("/login", data={"password": "pw-" + "y" * 12,
                                    "csrf": re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)})
        page = client.get("/connections")
        assert "/feed/" in page.text and "me@example" in page.text and "Test-E-Mail senden" in page.text
