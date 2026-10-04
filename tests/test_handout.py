import json

import pytest
from pypdf import PdfReader

from app.latex import LatexRejected, build_document, check_body, compile_pdf, expand_plots
from app.models import EpisodeOptions, EpisodeRequest, Length, ResearchDepth
from app.pipeline import create_job, load_context, run_pipeline
from app.prompts import PromptStore
from tests.conftest import FakeClaude, FakeTTS, default_responses, mock_downloads


def handout_job(settings, handout=True):
    request = EpisodeRequest(topic="Transformer", options=EpisodeOptions(
        length=Length.kurz, research_depth=ResearchDepth.quick, handout=handout))
    return create_job(settings, request, PromptStore(settings.prompts_dir))


def pdf_text(path) -> str:
    return "".join(page.extract_text() for page in PdfReader(path).pages)


async def test_handout_pipeline(settings):
    job_dir = handout_job(settings)
    claude = FakeClaude(default_responses())
    ctx = load_context(job_dir, settings, claude=claude, tts=FakeTTS())
    ctx.download_options = mock_downloads()
    stages = []
    await run_pipeline(ctx, progress=lambda s, m: stages.append(s))
    assert "handout" in stages and stages.index("handout") < stages.index("tts")

    (call,) = claude.calls_for("Handout")
    assert call.skills == ["latex-handout", "handout-plots"] and call.tools == ["Read"]
    assert "Testepisode" in call.prompt and "papers/arxiv_2401.00001.md" in call.prompt
    (fix,) = claude.calls_for("PlotFixes")
    assert "Import not allowed: os" in fix.prompt and fix.resume
    assert claude.calls_for("HandoutFix") == []

    figures = job_dir / "handout" / "figures"
    assert sorted(p.name for p in figures.iterdir()) == ["ergebnis.pdf", "zweite.pdf"]
    tex = (job_dir / "handout" / "handout.tex").read_text()
    assert "\\includegraphics[width=0.92\\linewidth]{figures/ergebnis.pdf}" in tex
    assert "\\section{Abbildungen}" in tex  # the repaired, unreferenced plot is appended
    text = pdf_text(job_dir / "handout.pdf")
    assert "Testepisode" in text and "Die Kernidee" in text and "PAPER PODCAST" in text.upper()
    assert "1 / 2" in text and "2 / 2" in text  # page count resolved in the second pdflatex run


async def test_latex_errors_are_repaired(settings):
    responses = default_responses()
    broken = dict(responses["Handout"][0], latex_body="\\section{Kaputt}\nText mit \\begin{itemize} ohne Ende\n")
    responses["Handout"] = [broken]
    responses["HandoutFix"] = [
        {"latex_body": "\\section{Noch kaputt}\n\\input{/etc/passwd}"},  # rejected by the check
        {"latex_body": "\\section{Repariert}\nJetzt passt alles."},
    ]
    job_dir = handout_job(settings)
    claude = FakeClaude(responses)
    ctx = load_context(job_dir, settings, claude=claude, tts=FakeTTS())
    ctx.download_options = mock_downloads()
    await run_pipeline(ctx)

    first, second = claude.calls_for("HandoutFix")
    assert "LaTeX Error" in first.prompt and "Line" in first.prompt
    assert "Security check" in second.prompt and "\\input" in second.prompt
    assert "Repariert" in pdf_text(job_dir / "handout.pdf")
    log = [json.loads(line) for line in (job_dir / "log.jsonl").read_text().splitlines()]
    (entry,) = [e for e in log if e["event"] == "handout"]
    assert len(entry["latex_errors"]) == 2


async def test_handout_fails_after_repairs(settings):
    responses = default_responses()
    responses["Handout"] = [dict(responses["Handout"][0], latex_body="\\badmacro")]
    responses["HandoutFix"] = [{"latex_body": "\\badmacro"}] * 2
    job_dir = handout_job(settings)
    ctx = load_context(job_dir, settings, claude=FakeClaude(responses), tts=FakeTTS())
    ctx.download_options = mock_downloads()
    with pytest.raises(RuntimeError, match="Handout ließ sich auch nach 2 Korrekturen nicht"):
        await run_pipeline(ctx)
    assert not (job_dir / "handout.pdf").exists()


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


@pytest.mark.parametrize("body", [
    "\\input{/etc/passwd}", "\\include{x}", "\\immediate\\write18{id}", "\\newcommand{\\x}{y}",
    "\\def\\x{y}", "\\usepackage{shellesc}", "^^5cinput{x}", "\\csname input\\endcsname",
    "\\includegraphics{/etc/passwd}", "\\end{document}", "\\catcode`\\@=11", "\\openin5=/etc/passwd",
])
def test_dangerous_latex_is_rejected(body):
    with pytest.raises(LatexRejected):
        check_body(body)


def test_harmless_latex_passes():
    check_body("\\section{Definition}\n\\begin{description}\\item[Default] x\\end{description} \\url{https://a.b}"
               " \\textbf{Lettering} $\\delta$")


def test_compiler_cannot_read_outside_files(tmp_path):
    secret = tmp_path / "secret.tex"
    secret.write_text("GEHEIMNIS")
    tex = build_document("P", "T", "d", f"\\input{{{secret}}}")  # bypasses check_body on purpose
    result = compile_pdf(tex, tmp_path / "none", tmp_path / "out.pdf")
    assert not result.ok and not (tmp_path / "out.pdf").exists()


def test_title_is_escaped(tmp_path):
    tex = build_document("Pod & Co", "50 % mehr_Tempo #1", "d", expand_plots("Text", {}, set()))
    assert compile_pdf(tex, tmp_path / "none", tmp_path / "out.pdf").ok
    assert "50 % mehr_Tempo #1" in pdf_text(tmp_path / "out.pdf")
