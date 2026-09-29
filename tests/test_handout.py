import json

from pypdf import PdfReader

from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.pipeline import create_job, load_context, run_pipeline
from app.pipeline.handout import build_html, write_pdf
from app.prompts import PromptStore
from tests.conftest import FakeClaude, FakeTTS, default_responses, mock_downloads


def handout_job(settings, handout=True):
    request = EpisodeRequest(topic="Transformer", options=EpisodeOptions(
        length=Length.kurz, research_depth=ResearchDepth.quick, handout=handout))
    job_dir = create_job(settings, request, PromptStore(settings.prompts_dir))
    return job_dir


async def test_handout_pipeline(settings):
    job_dir = handout_job(settings)
    claude = FakeClaude(default_responses())
    ctx = load_context(job_dir, settings, claude=claude, tts=FakeTTS())
    ctx.download_options = mock_downloads()
    stages = []
    await run_pipeline(ctx, progress=lambda s, m: stages.append(s))
    assert "handout" in stages and stages.index("handout") < stages.index("tts")

    (call,) = claude.calls_for("Handout")
    assert call.skills == ["handout-plots"] and call.tools == ["Read"]
    assert "Testepisode" in call.prompt and "papers/arxiv_2401.00001.md" in call.prompt
    (fix,) = claude.calls_for("PlotFixes")
    assert "Import nicht erlaubt: os" in fix.prompt and fix.resume

    figures = job_dir / "handout" / "figures"
    assert sorted(p.name for p in figures.iterdir()) == ["ergebnis.png", "formula-attention.png", "zweite.png"]
    html = (job_dir / "handout" / "handout.html").read_text()
    assert '<img src="figures/ergebnis.png"' in html and "Ergebnis A (Muster 2024)" in html
    assert '<pre class="formula-src">\\unknowncommand{</pre>' in html  # failed formula shown as source
    assert "<h2>Abbildungen</h2>" in html  # the repaired, unreferenced plot is appended

    text = "".join(page.extract_text() for page in PdfReader(job_dir / "handout.pdf").pages)
    assert "Handout zur Testepisode" in text and "Paper Podcast" in text

    log = [json.loads(line) for line in (job_dir / "log.jsonl").read_text().splitlines()]
    (entry,) = [e for e in log if e["event"] == "handout"]
    assert entry["plot_errors"] == {} and entry["formula_errors"] == ["kaputt"]


async def test_no_handout_when_disabled(settings):
    job_dir = handout_job(settings, handout=False)
    responses = default_responses()
    responses["Script"] = responses["Script"] * 2
    claude = FakeClaude(responses)
    ctx = load_context(job_dir, settings, claude=claude, tts=FakeTTS())
    ctx.download_options = mock_downloads()
    await run_pipeline(ctx)
    await run_pipeline(ctx, from_stage="script")  # forcing later stages must not force the handout
    assert claude.calls_for("Handout") == [] and not (job_dir / "handout.pdf").exists()


def test_pdf_blocks_external_resources(tmp_path):
    (tmp_path / "figures").mkdir()
    secret = tmp_path.parent / "secret.png"
    secret.write_bytes(b"x")
    html = ('<html><body><h1>T</h1><img src="https://example.org/x.png">'
            f'<img src="file://{secret}"><img src="../secret.png"></body></html>')
    write_pdf(html, tmp_path, tmp_path / "out.pdf")  # must not raise or fetch
    assert (tmp_path / "out.pdf").stat().st_size > 500


def test_markdown_raw_html_is_escaped():
    from app.models import Handout

    handout = Handout(markdown="# T\n\n<script>alert(1)</script>\n\n{{plot:nope}}", plots=[], formulas=[])
    html = build_html("Pod", "T", handout, None, set(), set())
    assert "<script>" not in html and "{{plot:nope}}" not in html
