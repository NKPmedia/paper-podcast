import json

import pytest

from app.models import Candidate, EpisodeOptions, EpisodeRequest, Length, RankedCandidate, ResearchDepth
from app.papers import DownloadError, check_public_https, download_all, html_to_markdown, pdf_to_markdown
from app.pipeline import create_job, load_context, run_pipeline
from app.pipeline.research import candidate_id, merge_candidates
from app.prompts import PromptStore
from tests.conftest import ARXIV_HTML, FakeClaude, FakeTTS, default_responses, mock_downloads


def make_pdf(text: str, lines: int = 1) -> bytes:
    """A minimal one-page PDF repeating ``text`` on ``lines`` lines."""
    body = " ".join(f"({text}) Tj 0 -14 Td" for _ in range(lines))
    stream = f"BT /F1 8 Tf 20 780 Td {body} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


# --- candidates -------------------------------------------------------------------


def test_candidate_ids():
    assert candidate_id(Candidate(title="A", arxiv_id="arXiv:1706.03762v5", score=1, reason="")) == "arxiv:1706.03762"
    assert candidate_id(Candidate(title="A", doi="10.48550/arXiv.1706.03762", score=1, reason="")) == "arxiv:1706.03762"
    assert candidate_id(Candidate(title="A", url="https://arxiv.org/abs/2401.12345", score=1, reason="")) == "arxiv:2401.12345"
    assert candidate_id(Candidate(title="A", doi="https://doi.org/10.1000/ABC", score=1, reason="")) == "doi:10.1000/abc"
    assert candidate_id(Candidate(title="Hello, World!", score=1, reason="")) == "title:hello-world"


def test_merge_deduplicates_and_ranks():
    results = {
        "background": [
            Candidate(title="Attention Is All You Need", arxiv_id="1706.03762", score=7, reason="bg"),
            Candidate(title="Low paper", score=2, reason="meh"),
        ],
        "core": [
            Candidate(title="Attention is all you need.", doi="10.5555/3295222", authors="Vaswani",
                      score=9, reason="core", quote="We propose"),
            Candidate(title="Other", score=8, reason="x"),
        ],
    }
    ranked = merge_candidates(results)
    assert [r.title for r in ranked][:2] == ["Attention Is All You Need", "Other"]
    top = ranked[0]
    assert top.id == "arxiv:1706.03762"
    assert top.found_by == ["background", "core"]
    assert top.score == 9 and top.reason == "core" and top.quote == "We propose"
    assert top.authors == "Vaswani" and top.doi == "10.5555/3295222"
    assert len(ranked) == 3


# --- conversion -------------------------------------------------------------------


def test_html_to_markdown_keeps_math_and_drops_junk():
    md = html_to_markdown(ARXIV_HTML)
    assert md.startswith("# Ein Paper")
    assert "## 1 Introduction" in md
    assert "$O(n^{2})$" in md
    assert "| Ours | 28.4 |" in md
    assert "Navigation" not in md and "var a" not in md and "reference list" not in md
    assert max(len(line) for line in md.splitlines()) <= 200


def test_pdf_to_markdown():
    md = pdf_to_markdown(make_pdf("Hello transformer world"))
    assert "Hello transformer world" in md and "Seite 1" in md


def test_url_safety():
    with pytest.raises(DownloadError):
        check_public_https("http://example.org/a.pdf")
    with pytest.raises(DownloadError):
        check_public_https("https://127.0.0.1/secret")
    with pytest.raises(DownloadError):
        check_public_https("file:///etc/passwd")


# --- downloads -------------------------------------------------------------------


def ranked(**kw) -> RankedCandidate:
    return RankedCandidate(title=kw.pop("title", "Ein Paper"), score=5, reason="r", **kw)


async def download(candidate, routes, **kw):
    opts = mock_downloads(routes)
    return (await download_all([candidate], kw.pop("out"), max_bytes=kw.pop("max_bytes", 10**6),
                               max_chars=kw.pop("max_chars", 10**6), timeout_s=5, **opts))[0]


async def test_arxiv_html_preferred(tmp_path):
    routes = {"https://arxiv.org/html/2401.00001": (200, "text/html", ARXIV_HTML.encode())}
    paper = await download(ranked(id="arxiv:2401.00001", arxiv_id="2401.00001"), routes, out=tmp_path)
    assert paper.format == "html" and paper.file == "papers/arxiv_2401.00001.md" and not paper.error
    text = (tmp_path / "arxiv_2401.00001.md").read_text()
    assert text.startswith("# Ein Paper\n\nQuelle: https://arxiv.org/html/2401.00001")
    assert paper.lines == text.count("\n")


async def test_fallback_to_pdf_after_redirect(tmp_path):
    pdf = make_pdf("The model reaches twenty eight BLEU on the benchmark data.", lines=50)
    routes = {
        "https://arxiv.org/pdf/2401.00001": (302, "", b"https://export.arxiv.org/pdf/2401.00001"),
        "https://export.arxiv.org/pdf/2401.00001": (200, "application/pdf", pdf),
    }
    paper = await download(ranked(id="arxiv:2401.00001", arxiv_id="2401.00001"), routes, out=tmp_path)
    assert paper.format == "pdf" and paper.source_url == "https://export.arxiv.org/pdf/2401.00001"
    assert "twenty eight BLEU" in (tmp_path / "arxiv_2401.00001.md").read_text()


async def test_too_little_text_is_rejected(tmp_path):
    routes = {"https://example.org/abs": (200, "text/html", b"<html><body><p>Just an abstract.</p></body></html>")}
    paper = await download(ranked(id="title:x", url="https://example.org/abs"), routes, out=tmp_path)
    assert not paper.file and "too little text" in paper.error


async def test_doi_via_openalex_and_truncation(tmp_path):
    long_html = ARXIV_HTML.replace("Result A", "Result B")
    routes = {
        "https://api.openalex.org/works/doi:10.1/x": (
            200, "application/json",
            json.dumps({"best_oa_location": {"pdf_url": "https://oa.example.org/x"}}).encode(),
        ),
        "https://oa.example.org/x": (200, "text/html", long_html.encode()),
    }
    paper = await download(ranked(id="doi:10.1/x", doi="10.1/x"), routes, out=tmp_path, max_chars=3000)
    assert paper.source_url == "https://oa.example.org/x" and paper.truncated
    assert "[… gekürzt …]" in (tmp_path / "doi_10.1_x.md").read_text()


async def test_size_limit(tmp_path):
    routes = {"https://example.org/big.pdf": (200, "application/pdf", b"%PDF-" + b"0" * 5000)}
    paper = await download(ranked(id="title:big", pdf_url="https://example.org/big.pdf"), routes,
                           out=tmp_path, max_bytes=1000)
    assert "too large" in paper.error


# --- research stage --------------------------------------------------------------


def job(settings, depth=ResearchDepth.medium):
    request = EpisodeRequest(topic="Transformer", options=EpisodeOptions(length=Length.kurz, research_depth=depth))
    return create_job(settings, request, PromptStore(settings.prompts_dir))


def ctx_for(settings, job_dir, claude, routes=None):
    ctx = load_context(job_dir, settings, claude=claude, tts=FakeTTS())
    ctx.download_options = mock_downloads(routes)
    return ctx


async def test_medium_research_flow(settings):
    responses = default_responses()

    def scout(call):
        if "Kritik und Einordnung" in call.prompt:
            raise RuntimeError("scout crashed")
        return {"candidates": [{"title": "Ein Paper", "arxiv_id": "2401.00001", "score": 9, "reason": "Kern"},
                               {"title": "Blogpost", "url": "https://blog.example.org/p", "score": 3, "reason": "B"}]}

    responses["ScoutResult"] = [scout] * 3
    responses["Selection"] = [{"focus": "F", "selected": [
        {"id": "arxiv:2401.00001", "reason": "Kern"},
        {"id": "title:blogpost", "reason": "Einordnung"},
        {"id": "erfunden:1", "reason": "gibt es nicht"},
    ]}]
    claude = FakeClaude(responses)
    job_dir = job(settings)
    ctx = ctx_for(settings, job_dir, claude)
    notes = []
    await run_pipeline(ctx, progress=lambda stage, msg: notes.append(msg))

    scouts = claude.calls_for("ScoutResult")
    assert len(scouts) == 3 and all(c.model == "haiku" for c in scouts)
    assert sorted(p.name for p in (job_dir / "scouts").iterdir()) == ["background.json", "core.json"]
    log = [json.loads(line) for line in (job_dir / "log.jsonl").read_text().splitlines()]
    assert any(e["event"] == "scout_failed" and e["angle"] == "critique" for e in log)

    candidates = json.loads((job_dir / "candidates.json").read_text())
    assert [c["id"] for c in candidates] == ["arxiv:2401.00001", "title:blogpost"]
    assert candidates[0]["found_by"] == ["background", "core"]

    selection = json.loads((job_dir / "selection.json").read_text())
    assert [s["id"] for s in selection["selected"]] == ["arxiv:2401.00001", "title:blogpost"]

    index = json.loads((job_dir / "papers/index.json").read_text())
    assert index[0]["file"] and not index[1]["file"] and "HTTP 404" in index[1]["error"]

    (read,) = claude.calls_for("ResearchResult")
    assert read.model == "opus" and read.tools == ["Read", "WebSearch", "WebFetch"]
    assert "papers/arxiv_2401.00001.md" in read.prompt
    assert "Nutze WebFetch mit gezielten Fragen: https://blog.example.org/p" in read.prompt
    assert any("Scout" in n for n in notes) and any("liest 1 Paper" in n for n in notes)


async def test_research_resumes_and_resets(settings):
    claude = FakeClaude(default_responses())
    job_dir = job(settings, ResearchDepth.quick)
    ctx = ctx_for(settings, job_dir, claude)
    await run_pipeline(ctx)

    # Simulate a crash during reading: only the final notes are missing.
    (job_dir / "research.md").unlink()
    claude.responses["ResearchResult"] = [default_responses()["ResearchResult"][0]]
    await run_pipeline(ctx)
    assert len(claude.calls_for("ScoutResult")) == 1 and len(claude.calls_for("ResearchResult")) == 2

    # Forcing the research stage starts from scratch.
    fresh = default_responses()
    claude.responses.update({k: fresh[k] for k in ("ScoutResult", "Selection", "ResearchResult", "Script")})
    await run_pipeline(ctx, from_stage="research")
    assert len(claude.calls_for("ScoutResult")) == 2 and len(claude.calls_for("Selection")) == 2


async def test_all_scouts_failing_is_an_error(settings):
    def boom(call):
        raise RuntimeError("down")

    responses = default_responses()
    responses["ScoutResult"] = [boom] * 3
    ctx = ctx_for(settings, job(settings), FakeClaude(responses))
    with pytest.raises(RuntimeError, match="Alle Scouts"):
        await run_pipeline(ctx)


async def test_selection_with_only_unknown_ids_falls_back_to_ranking(settings):
    responses = default_responses()
    responses["Selection"] = [{"focus": "F", "selected": [{"id": "nope", "reason": "x"}]}]
    job_dir = job(settings, ResearchDepth.quick)
    await run_pipeline(ctx_for(settings, job_dir, FakeClaude(responses)))
    selection = json.loads((job_dir / "selection.json").read_text())
    assert [s["id"] for s in selection["selected"]] == ["arxiv:2401.00001", "doi:10.1000/xyz"]
