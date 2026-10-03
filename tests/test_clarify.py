import json

from app.models import ClarificationRequest, EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.pipeline import clarify
from tests.conftest import FakeClaude, default_responses
from tests.test_telegram import CHAT, buttons, env, run_next  # noqa: F401  (fixture)
from tests.test_web import csrf, login, make_client, web_settings  # noqa: F401  (fixture)

SHORT = EpisodeOptions(length=Length.kurz, research_depth=ResearchDepth.quick)
QUESTIONS = {
    "needs_clarification": True,
    "reason": "Mamba names several things.",
    "questions": [
        {"question": "Welches Mamba ist gemeint?", "options": ["Das Sprachmodell (Gu & Dao 2023)", "Die Schlange"],
         "allow_free_text": True},
        {"question": "Für wen ist die Folge?", "options": ["Einsteiger", "Fachleute"], "allow_free_text": False},
    ],
}


def responses_with_questions():
    responses = default_responses()
    responses["ClarificationRequest"] = [QUESTIONS]
    return responses


def test_clean_limits_and_normalizes():
    many = ClarificationRequest(needs_clarification=True, reason="r", questions=[
        {"question": f"Frage {i}", "options": ["a", " ", "b"]} for i in range(14)
    ] + [{"question": "", "options": ["x"]}])
    data = clarify._clean(many)
    assert len(data["questions"]) == 10 and data["questions"][0]["options"] == ["a", "b"]
    assert data["answers"] is None
    clear = clarify._clean(ClarificationRequest(needs_clarification=True, reason="r", questions=[]))
    assert clear["needs_clarification"] is False and clear["answers"] == []


async def test_job_waits_for_answers_and_uses_them(env):  # noqa: F811
    env.worker.context_factory = lambda d: _ctx(env, d, responses_with_questions())
    job = env.service.submit(EpisodeRequest(topic="Mamba", options=SHORT), origin="web")

    waiting = await run_next(env)
    assert waiting.status == "waiting" and "2 Rückfragen" in waiting.message
    job_dir = env.service.job_dir(job.id)
    assert not (job_dir / "candidates.json").exists()  # nothing researched yet
    assert len(clarify.pending_questions(job_dir)) == 2

    env.service.answer(job.id, ["Das Sprachmodell (Gu & Dao 2023)", ""])
    assert env.store.get(job.id).status == "queued"
    claude = FakeClaude(default_responses())
    env.worker.context_factory = lambda d: _ctx(env, d, claude=claude)
    done = await run_next(env)
    assert done.status == "done"
    assert claude.calls_for("ClarificationRequest") == []  # not asked twice
    scout = claude.calls_for("ScoutResult")[0]
    assert "Answers from the listener" in scout.prompt and "Gu & Dao 2023" in scout.prompt
    assert "decide sensibly yourself" in scout.prompt
    assert "Gu & Dao 2023" in claude.calls_for("Script")[0].prompt
    events = [json.loads(line)["event"] for line in (job_dir / "log.jsonl").read_text().splitlines()]
    assert {"clarify", "waiting", "answers"} <= set(events)


async def test_clarify_switched_off(env):  # noqa: F811
    claude = FakeClaude(responses_with_questions())
    env.worker.context_factory = lambda d: _ctx(env, d, claude=claude)
    env.service.submit(EpisodeRequest(topic="Mamba", options=SHORT.model_copy(update={"clarify": False})))
    assert (await run_next(env)).status == "done"
    assert claude.calls_for("ClarificationRequest") == []


async def test_telegram_asks_questions_one_by_one(env):  # noqa: F811
    env.worker.context_factory = lambda d: _ctx(env, d, responses_with_questions())
    await env.bot.handle_text(CHAT, "Mamba")
    draft = env.m.sent[-1]
    assert "d:cl" in buttons(draft)
    await env.bot.handle_callback(CHAT, draft["id"], "cb", "d:go")
    job = (await run_next(env))
    assert job.status == "waiting"

    first = env.m.sent[-1]
    assert "Rückfrage 1/2" in first["text"] and "Welches Mamba" in first["text"]
    assert "q:0:0" in buttons(first) and "eigene Antwort" in first["text"]
    await env.bot.handle_text(CHAT, "Mamba als Hardware-Beschleuniger")  # free text answer
    second = env.m.edits[-1]
    assert "Rückfrage 2/2" in second["text"] and "eigene Antwort" not in second["text"]
    await env.bot.handle_callback(CHAT, first["id"], "cb", "q:0:1")  # stale button
    assert "Abgelaufen" in env.m.answers[-1]
    await env.bot.handle_callback(CHAT, first["id"], "cb", "q:1:1")

    assert env.store.get(job.id).status == "queued"
    assert any("Danke!" in e["text"] and "Fachleute" in e["text"] for e in env.m.edits)
    answers = clarify.load(env.service.job_dir(job.id))["answers"]
    assert [a["answer"] for a in answers] == ["Mamba als Hardware-Beschleuniger", "Fachleute"]
    assert CHAT not in env.bot.sessions


def test_web_answer_form(web_settings):  # noqa: F811
    import asyncio

    with make_client(web_settings, start_worker=True, responses=responses_with_questions()) as client:
        login(client)
        response = client.post("/episodes", data={"csrf": csrf(client), "topic": "Mamba", "length": "kurz",
                                                  "depth": "quick", "clarify": "on"})
        job_id = response.url.path.rsplit("/", 1)[1]
        for _ in range(200):
            if client.get(f"/episodes/{job_id}/status.json").json()["status"] == "waiting":
                break
            asyncio.run(asyncio.sleep(0.05))
        page = client.get(f"/episodes/{job_id}")
        assert "Rückfragen von Claude" in page.text and "Welches Mamba ist gemeint?" in page.text
        assert 'name="q0_text"' in page.text and 'name="q1_text"' not in page.text  # free text only where allowed

        client.post(f"/episodes/{job_id}/answers", data={
            "csrf": csrf(client, f"/episodes/{job_id}"), "q0": "Die Schlange", "q0_text": "", "q1": "Einsteiger"})
        for _ in range(200):
            status = client.get(f"/episodes/{job_id}/status.json").json()
            if status["status"] == "done":
                break
            asyncio.run(asyncio.sleep(0.05))
        assert status["status"] == "done"
        page = client.get(f"/episodes/{job_id}")
        assert "Rückfragen (2)" in page.text and "Die Schlange" in page.text and "Antworten erhalten" in page.text


def _ctx(env, job_dir, responses=None, claude=None):
    from app.pipeline import load_context
    from tests.conftest import FakeTTS, mock_downloads

    ctx = load_context(job_dir, env.settings, claude=claude or FakeClaude(responses or default_responses()),
                       tts=FakeTTS())
    ctx.download_options = mock_downloads()
    return ctx
