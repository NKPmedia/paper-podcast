import json
from types import SimpleNamespace

import pytest

from app.db import JobStore
from app.jobs import JobService, Worker
from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.pipeline import load_context
from app.prompts import PromptStore
from app.telegram_bot import TelegramBot
from tests.conftest import FakeClaude, FakeTTS, default_responses, mock_downloads

CHAT = 4242


class FakeMessenger:
    def __init__(self):
        self.sent, self.edits, self.audio, self.answers, self.documents = [], [], [], [], []
        self._next_id = 100

    async def send_text(self, chat_id, text, keyboard=None):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "id": self._next_id, "text": text, "keyboard": keyboard})
        return self._next_id

    async def edit_text(self, chat_id, message_id, text, keyboard=None):
        self.edits.append({"chat_id": chat_id, "id": message_id, "text": text, "keyboard": keyboard})

    async def send_audio(self, chat_id, path, *, file_id, title, performer, duration, caption):
        self.audio.append({"chat_id": chat_id, "path": path, "file_id": file_id, "title": title, "caption": caption})
        return file_id or "FILE123"

    async def send_document(self, chat_id, path, *, file_id, filename, caption):
        self.documents.append({"chat_id": chat_id, "path": path, "file_id": file_id, "filename": filename})
        return file_id or "DOC1"

    async def answer_callback(self, callback_id, text=""):
        self.answers.append(text)


@pytest.fixture
def env(settings):
    settings = settings.model_copy(update={"telegram_allowed_chat_ids": str(CHAT), "public_base_url": "https://pod.example"})
    store = JobStore(settings.db_path)
    service = JobService(settings, store, PromptStore(settings.prompts_dir))

    def factory(job_dir, responses=None):
        ctx = load_context(job_dir, settings, claude=FakeClaude(responses or default_responses()), tts=FakeTTS())
        ctx.download_options = mock_downloads()
        return ctx

    worker = Worker(service, factory)
    messenger = FakeMessenger()
    bot = TelegramBot(settings, service, worker, messenger)
    worker.listeners.append(bot)
    return SimpleNamespace(settings=settings, store=store, service=service, worker=worker, bot=bot, m=messenger)


async def run_next(env):
    job = env.store.claim_next()
    await env.worker.run_job(job)
    return env.store.get(job.id)


def buttons(message):
    return [data for row in message["keyboard"] or [] for _, data in row]


async def test_setup_mode_and_access_control(env):
    await env.bot.handle_text(999, "Hallo")
    assert env.m.sent == []  # unknown chat is ignored when an allow-list exists

    env.bot.settings = env.settings.model_copy(update={"telegram_allowed_chat_ids": ""})
    await env.bot.handle_command(999, "start", [])
    assert "Deine Chat-ID: <code>999</code>" in env.m.sent[-1]["text"]


async def test_new_episode_flow_until_audio(env):
    await env.bot.handle_text(CHAT, "Das <Mamba> Paper")
    draft = env.m.sent[-1]
    assert "Das &lt;Mamba&gt; Paper" in draft["text"]
    assert {"d:len:kurz", "d:dep:quick", "d:go", "d:x"} <= set(buttons(draft))

    await env.bot.handle_callback(CHAT, draft["id"], "cb1", "d:len:kurz")
    await env.bot.handle_callback(CHAT, draft["id"], "cb2", "d:dep:quick")
    assert "✓ Kurz" in str(env.m.edits[-1]["keyboard"]) and "✓ Schnell" in str(env.m.edits[-1]["keyboard"])
    await env.bot.handle_callback(CHAT, draft["id"], "cb3", "d:go")

    (job,) = env.store.list()
    assert job.origin == "telegram" and job.status == "queued"
    request = json.loads((env.service.job_dir(job.id) / "request.json").read_text())
    assert request["request"]["options"]["length"] == "kurz"
    assert request["request"]["options"]["research_depth"] == "quick"
    assert "In der Warteschlange" in env.m.edits[-1]["text"]
    assert f"c:{job.id}" in buttons(env.m.edits[-1])

    sent_before = len(env.m.sent)
    done = await run_next(env)
    assert done.status == "done"
    # progress edits went to the draft message, no duplicate announcement was sent
    progress = [e for e in env.m.edits if e["id"] == draft["id"]]
    assert any("▶ <b>Recherche</b>" in e["text"] for e in progress)
    assert any("✓ Recherche · ✓ Skript · ▶ <b>Sprache</b>" in e["text"] for e in progress)
    assert progress[-1]["text"].startswith("✅ <b>Testepisode</b>")
    assert "https://pod.example/episodes/" in progress[-1]["text"]

    (audio,) = env.m.audio
    assert audio["path"].name == "episode.mp3" and audio["file_id"] is None
    assert audio["title"] == "Testepisode" and "Eine kurze Testepisode." in audio["caption"]
    chapters = env.m.sent[sent_before:]
    assert len(chapters) == 1 and "0:00 Kapitel 1" in chapters[0]["text"] and "Ein Paper (2024)" in chapters[0]["text"]

    # Re-sending uses the cached Telegram file id.
    await env.bot.handle_command(CHAT, "folge", ["1"])
    assert env.m.audio[-1]["file_id"] == "FILE123" and env.m.audio[-1]["path"] is None


async def test_draft_discard_and_expired(env):
    await env.bot.handle_command(CHAT, "neu", ["Quantencomputer"])
    draft = env.m.sent[-1]
    await env.bot.handle_callback(CHAT, draft["id"], "cb", "d:x")
    assert env.m.edits[-1]["text"] == "Verworfen: Quantencomputer"
    await env.bot.handle_callback(CHAT, draft["id"], "cb", "d:go")
    assert "Abgelaufen" in env.m.answers[-1]
    assert env.store.list() == []

    await env.bot.handle_command(CHAT, "neu", [])
    assert "Worum soll es gehen?" in env.m.sent[-1]["text"]


async def test_web_job_is_announced_and_delivered(env):
    env.service.submit(EpisodeRequest(topic="Vom Web", options=EpisodeOptions(length=Length.kurz,
                                                                         research_depth=ResearchDepth.quick)))
    await run_next(env)
    announce = env.m.sent[0]
    assert announce["chat_id"] == CHAT and "Vom Web" in announce["text"]
    assert env.m.edits[-1]["id"] == announce["id"] and env.m.edits[-1]["text"].startswith("✅")
    assert len(env.m.audio) == 1

    env.bot.settings = env.settings.model_copy(update={"telegram_notify_all": False})
    env.m.sent.clear()
    env.service.submit(EpisodeRequest(topic="Leise", options=EpisodeOptions(length=Length.kurz,
                                                                       research_depth=ResearchDepth.quick)))
    await run_next(env)
    assert env.m.sent == [] and len(env.m.audio) == 1


async def test_failed_job_can_be_resumed_from_telegram(env):
    responses = default_responses()
    responses["ScoutResult"] = [lambda call: (_ for _ in ()).throw(RuntimeError("kaputt"))]
    env.worker.context_factory = lambda d: (lambda ctx: (setattr(ctx, "download_options", mock_downloads()), ctx)[1])(
        load_context(d, env.settings, claude=FakeClaude(responses), tts=FakeTTS()))
    env.service.submit(EpisodeRequest(topic="Wird scheitern"))
    failed = await run_next(env)
    assert failed.status == "failed"
    last = env.m.edits[-1]
    assert last["text"].startswith("❌") and "Alle Scouts" in last["text"]
    assert buttons(last) == [f"r:{failed.id}"]

    await env.bot.handle_callback(CHAT, last["id"], "cb", f"r:{failed.id}")
    assert env.store.get(failed.id).status == "queued"
    assert env.m.answers[-1] == "Wird fortgesetzt" and env.m.edits[-1]["text"].startswith("⏳")


async def test_list_current_status_cancel(env):
    await env.bot.handle_command(CHAT, "aktuell", [])
    assert "Noch keine fertige Episode" in env.m.sent[-1]["text"]
    await env.bot.handle_command(CHAT, "liste", [])
    assert "Noch keine fertigen" in env.m.sent[-1]["text"]
    await env.bot.handle_command(CHAT, "status", [])
    assert "Nichts in Arbeit" in env.m.sent[-1]["text"]
    await env.bot.handle_command(CHAT, "abbrechen", [])
    assert "kein Job" in env.m.sent[-1]["text"]
    await env.bot.handle_command(CHAT, "folge", ["7"])
    assert "Nummer aus /liste" in env.m.sent[-1]["text"]
    await env.bot.handle_command(CHAT, "quatsch", [])
    assert env.m.sent[-1]["text"].startswith("Unbekannter Befehl")

    env.service.submit(EpisodeRequest(topic="Erste", options=EpisodeOptions(length=Length.kurz, research_depth=ResearchDepth.quick)))
    await run_next(env)
    env.service.submit(EpisodeRequest(topic="Zweite wartet"))
    env.m.sent.clear(); env.m.audio.clear()

    await env.bot.handle_command(CHAT, "status", [])
    assert "⏳ Zweite wartet – Wartet" in env.m.sent[-1]["text"]
    await env.bot.handle_command(CHAT, "liste", [])
    assert "1. Testepisode (0:" in env.m.sent[-1]["text"]
    await env.bot.handle_command(CHAT, "current", [])  # English alias
    texts = [s["text"] for s in env.m.sent]
    assert any("Zweite wartet" in t and "In der Warteschlange" in t for t in texts)
    assert len(env.m.audio) == 1 and env.m.audio[0]["title"] == "Testepisode"


def test_ptb_adapter_dispatch(env):
    """The python-telegram-bot handlers parse updates and call the bot core (no network)."""
    import asyncio

    from app.telegram_bot import TelegramRunner

    settings = env.settings.model_copy(update={"telegram_bot_token": "123456:TEST"})
    runner = TelegramRunner(settings, env.service, env.worker)
    runner.core.messenger = env.m
    handlers = runner.application.handlers[0]
    on_command, on_text, on_callback = (h.callback for h in handlers)

    message = SimpleNamespace(text="/neu@PodBot Mamba", message_id=5, chat=SimpleNamespace(id=CHAT))
    update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=CHAT))
    asyncio.run(on_command(update, SimpleNamespace(args=None)))
    assert "<b>Neue Episode</b>\nMamba" in env.m.sent[-1]["text"]

    message.text = "Freitext-Thema"
    asyncio.run(on_text(update, SimpleNamespace(args=None)))
    draft = env.m.sent[-1]
    assert "Freitext-Thema" in draft["text"]

    query = SimpleNamespace(id="q1", data="d:len:lang",
                            message=SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=draft["id"]))
    asyncio.run(on_callback(SimpleNamespace(callback_query=query), None))
    assert "Lang (~25 min)" in env.m.edits[-1]["text"]
    assert runner.core in env.worker.listeners


def test_end_to_end_with_real_adapter(env):
    """Real python-telegram-bot polling against a fake Bot API, inside the web server's lifespan."""
    import socket

    from fastapi.testclient import TestClient

    from app.auth import hash_password
    from app.web import create_app
    from tests.fake_telegram import FakeTelegramServer

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    fake = FakeTelegramServer(port)
    fake.start()
    settings = env.settings.model_copy(update={
        "telegram_bot_token": "123456:TEST",
        "telegram_api_base_url": f"http://127.0.0.1:{port}",
        "web_password_hash": hash_password("x" * 12),
    })

    def factory(job_dir):
        ctx = load_context(job_dir, settings, claude=FakeClaude(default_responses()), tts=FakeTTS())
        ctx.download_options = mock_downloads()
        return ctx

    try:
        with TestClient(create_app(settings, context_factory=factory)):
            fake.wait_for(lambda c: c[0] == "setMyCommands")
            fake.push_message(CHAT, "/neu Mamba & Co")
            method, draft = fake.wait_for(lambda c: c[0] == "sendMessage" and "Neue Episode" in c[1]["text"])
            assert "Mamba &amp; Co" in draft["text"] and draft["parse_mode"] == "HTML"
            assert "d:go" in str(draft["reply_markup"])
            draft_id = 1001

            fake.push_callback(CHAT, draft_id, "d:len:kurz")
            fake.push_callback(CHAT, draft_id, "d:dep:quick")
            fake.push_callback(CHAT, draft_id, "d:go")
            _, audio = fake.wait_for(lambda c: c[0] == "sendAudio", timeout=60)
            assert audio["audio"]["filename"] == "episode.mp3" and audio["audio"]["size"] > 1000
            assert audio["title"] == "Testepisode" and audio["performer"] == "Paper Podcast"
            fake.wait_for(lambda c: c[0] == "sendMessage" and "<b>Kapitel</b>" in c[1]["text"])
            edits = [c[1]["text"] for c in fake.calls if c[0] == "editMessageText" and int(c[1]["message_id"]) == draft_id]
            assert any("▶ <b>Recherche</b>" in t for t in edits) and edits[-1].startswith("✅")

            # /folge 1 re-sends via the cached file id (no upload)
            fake.push_message(CHAT, "/folge 1")
            _, again = fake.wait_for(lambda c: c[0] == "sendAudio" and c[1].get("audio") == "AUDIO-FILE-ID")
            assert again["title"] == "Testepisode"
    finally:
        fake.stop()


async def test_handout_toggle_and_delivery(env):
    await env.bot.handle_text(CHAT, "Mit Handout")
    draft = env.m.sent[-1]
    assert "Handout: nein" in draft["text"] and "d:ho" in buttons(draft)
    await env.bot.handle_callback(CHAT, draft["id"], "cb", "d:len:kurz")
    await env.bot.handle_callback(CHAT, draft["id"], "cb", "d:dep:quick")
    await env.bot.handle_callback(CHAT, draft["id"], "cb", "d:ho")
    assert "Handout: ja (PDF)" in env.m.edits[-1]["text"]
    await env.bot.handle_callback(CHAT, draft["id"], "cb", "d:go")
    done = await run_next(env)
    assert done.status == "done"
    assert any("▶ <b>Handout</b>" in e["text"] for e in env.m.edits)
    (doc,) = env.m.documents
    assert doc["path"].name == "handout.pdf" and doc["filename"].startswith("Handout - ")
    await env.bot.handle_command(CHAT, "folge", ["1"])
    assert env.m.documents[-1]["file_id"] == "DOC1" and env.m.documents[-1]["path"] is None
