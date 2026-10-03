import asyncio
import json
import re
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.pipeline import load_context
from app.web import create_app
from tests.conftest import FakeClaude, FakeTTS, default_responses, mock_downloads

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

    asyncio.run(go())


def test_feed(settings):
    settings, app = make_app(settings)
    token = settings.feed_token
    with TestClient(app) as client:
        job_id = app.state.service.submit(EpisodeRequest(topic="Feed", options=EpisodeOptions(
            length=Length.kurz, research_depth=ResearchDepth.quick))).id
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


def test_asset_uploads(settings):
    import io
    import subprocess

    from PIL import Image

    settings, app = make_app(settings)
    with TestClient(app) as client:
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
        client.post("/login", data={"password": "pw-" + "y" * 12, "csrf": token})
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/connections").text).group(1)

        image = io.BytesIO()
        Image.new("RGB", (800, 600), "red").save(image, "PNG")
        client.post("/assets/cover", data={"csrf": token}, files={"file": ("c.png", image.getvalue())})
        cover = Image.open(settings.assets_dir / "cover.jpg")
        assert cover.size == (1400, 1400) and cover.format == "JPEG"

        wav = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-f", "wav", "-"],
                             capture_output=True, check=True).stdout
        client.post("/assets/intro", data={"csrf": token}, files={"file": ("i.wav", wav)})
        assert (settings.assets_dir / "intro.mp3").stat().st_size > 1000

        page = client.post("/assets/outro", data={"csrf": token}, files={"file": ("x.mp3", b"not audio")})
        assert "Keine gültige Audiodatei" in page.text and not (settings.assets_dir / "outro.mp3").exists()
        page = client.post("/assets/cover", data={"csrf": token}, files={"file": ("x.png", b"nope")})
        assert "Kein gültiges Bild" in page.text

        assert "Edge TTS" in page.text and client.get("/assets/cover").headers["content-type"] == "image/jpeg"
        client.post("/assets/intro/delete", data={"csrf": token})
        assert not (settings.assets_dir / "intro.mp3").exists()
