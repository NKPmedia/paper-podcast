import json

import pytest

from app.errors import PodcastError
from app.models import Script
from app.tts import make_tts, resolve_tts
from app.tts.edge import EdgeTTS
from app.tts.gemini import FallbackTTS, GeminiTTS
from tests.conftest import FakeTTS, make_script
from tests.test_web import csrf, login, make_client, web_settings  # noqa: F401


class Broken:
    def __init__(self, name, message="kaputt"):
        self.name, self.message, self.calls = name, message, 0

    async def synthesize(self, script, out_dir):
        self.calls += 1
        (out_dir / f"{self.name}_partial.mp3").write_bytes(b"x")
        raise PodcastError(self.message, "details")


def test_choice_and_fallback_direction(settings):
    assert resolve_tts(settings) == "edge" and resolve_tts(settings, "gemini") == "gemini"
    assert isinstance(make_tts(settings, choice="gemini"), EdgeTTS)  # no key: Edge only
    keyed = settings.model_copy(update={"gemini_api_key": "key"})
    assert resolve_tts(keyed) == "gemini"  # auto with a key
    edge_first = make_tts(keyed, choice="edge")
    assert isinstance(edge_first.primary, EdgeTTS) and isinstance(edge_first.fallback, GeminiTTS)
    gemini_first = make_tts(keyed, choice="gemini")
    assert isinstance(gemini_first.primary, GeminiTTS) and isinstance(gemini_first.fallback, EdgeTTS)


async def test_edge_problems_switch_to_gemini_and_resume_keeps_it(tmp_path):
    script = Script.model_validate(make_script(words_per_line=4))
    edge, gemini = Broken("edge", "Edge gedrosselt"), FakeTTS()
    gemini.name = "gemini"
    tts = FallbackTTS(edge, gemini)
    clips = await tts.synthesize(script, tmp_path)
    assert tts.name == "gemini" and "edge → gemini: Edge gedrosselt" == tts.note
    assert clips and not (tmp_path / "edge_partial.mp3").exists()  # no mixed voices
    assert (tmp_path / "engine.txt").read_text() == "gemini"

    # "Fortsetzen" after the switch: goes on with Gemini, Edge is not tried again.
    edge2, gemini2 = Broken("edge"), FakeTTS()
    gemini2.name = "gemini"
    resumed = FallbackTTS(edge2, gemini2)
    await resumed.synthesize(script, tmp_path)
    assert edge2.calls == 0 and gemini2.calls == 1 and "Fortgesetzt mit gemini" in resumed.note


async def test_both_engines_failing_names_both(tmp_path):
    tts = FallbackTTS(Broken("edge", "Edge gedrosselt"), Broken("gemini", "Kontingent erschöpft"))
    with pytest.raises(PodcastError) as error:
        await tts.synthesize(Script.model_validate(make_script()), tmp_path)
    assert error.value.message == ("Beide Sprachausgaben sind gescheitert. edge: Edge gedrosselt – "
                                   "Ersatz gemini: Kontingent erschöpft")


def test_tts_choice_in_the_form(web_settings):
    keyed = web_settings.model_copy(update={"gemini_api_key": "key", "tts_provider": "edge"})
    with make_client(keyed) as client:
        login(client)
        form = client.get("/").text
        assert 'value="edge" selected' in form and "Gemini springt bei Problemen ein" in form
        response = client.post("/episodes", data={"csrf": csrf(client), "topic": "X", "length": "kurz",
                                                  "depth": "quick", "tts": "gemini"})
        job_id = response.url.path.rsplit("/", 1)[1]
        options = json.loads((keyed.episodes_dir / job_id / "request.json").read_text())["request"]["options"]
        assert options["tts"] == "gemini"
        assert "Stimme: Gemini" in client.get(f"/episodes/{job_id}").text

    with make_client(web_settings) as client:  # without a key Gemini cannot be chosen
        login(client)
        assert "erst Gemini-Key unter Einstellungen" in client.get("/").text
