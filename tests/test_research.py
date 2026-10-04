import json

import pytest

from app.errors import PodcastError

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
    assert text.startswith("# Ein Paper\n\nSource: https://arxiv.org/html/2401.00001")
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
    text = (tmp_path / "doi_10.1_x.md").read_text()
    assert "TRUNCATED: the last" in text and "> Note: this file holds the main text" in text
    assert "The main text was cut" in text and paper.omitted_chars > 0


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
        if "Replications, limitations, opposing views" in call.prompt:
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

    (plan_call,) = claude.calls_for("ResearchPlan")
    assert "between 2 and 4 scout tasks" in plan_call.prompt and plan_call.model == "opus"
    assert json.loads((job_dir / "plan.json").read_text())["key_questions"][0] == "Was zeigt das Paper?"

    scouts = claude.calls_for("ScoutResult")
    assert len(scouts) == 3 and all(c.model == "haiku" for c in scouts)
    assert "# Your task: Core work" in scouts[0].prompt and "Was zeigt das Paper?" in scouts[0].prompt
    assert sorted(p.name for p in (job_dir / "scouts").iterdir()) == ["t1.json", "t2.json"]
    log = [json.loads(line) for line in (job_dir / "log.jsonl").read_text().splitlines()]
    assert any(e["event"] == "scout_failed" and e["angle"] == "Critique" for e in log)

    candidates = json.loads((job_dir / "candidates.json").read_text())
    assert [c["id"] for c in candidates] == ["arxiv:2401.00001", "title:blogpost"]
    assert candidates[0]["found_by"] == ["t1", "t2"]

    selection = json.loads((job_dir / "selection.json").read_text())
    assert [s["id"] for s in selection["selected"]] == ["arxiv:2401.00001", "title:blogpost"]
    (select_call,) = claude.calls_for("Selection")
    assert "reading budget of about 90k tokens" in select_call.prompt

    index = json.loads((job_dir / "papers/index.json").read_text())
    assert index[0]["file"] and not index[1]["file"] and "HTTP 404" in index[1]["error"]
    reading = json.loads((job_dir / "reading.json").read_text())
    assert [e["mode"] for e in reading] == ["full", "webfetch"] and reading[0]["tokens"] > 0

    (read,) = claude.calls_for("ResearchResult")  # one model reads everything, no reader agents
    assert read.model == "opus" and read.tools == ["Read", "Grep", "WebSearch", "WebFetch"]
    assert "papers/arxiv_2401.00001.md" in read.prompt and "Reading depth: **complete**" in read.prompt
    assert "Use WebFetch with specific questions (at most 4 fetches): https://blog.example.org/p" in read.prompt
    assert "Cross-check" in read.prompt and "Cite with location" in read.prompt
    assert any("Scout" in n for n in notes) and any("Claude liest 2 Paper (1 komplett" in n for n in notes)


def test_reading_budget_uses_measured_lengths():
    from app.pipeline.research import PROFILES, plan_reading, selective_tokens

    profile = PROFILES[ResearchDepth.medium]  # 90k budget, at most 5 papers
    papers = [
        {"id": "a", "file": "papers/a.md", "tokens": 30_000},  # core: always complete
        {"id": "b", "file": "papers/b.md", "tokens": 12_000},
        {"id": "c", "file": "", "tokens": 0},  # no full text: WebFetch
        {"id": "d", "file": "papers/d.md", "tokens": 20_000},
        {"id": "e", "file": "papers/e.md", "tokens": 35_000},  # too long for the rest: selective
        {"id": "f", "file": "papers/f.md", "tokens": 9_000},  # over the paper limit
    ]
    reading = plan_reading(papers, profile)
    assert [e["mode"] for e in reading] == ["full", "full", "webfetch", "full", "selective", "skipped"]
    assert sum(e["cost"] for e in reading) == 30_000 + 12_000 + 4_000 + 20_000 + selective_tokens(35_000)
    assert sum(e["cost"] for e in reading) <= profile.read_budget
    assert "Höchstzahl" in reading[-1]["reason"]

    # A huge core paper is still read completely; nothing else fits after it.
    many = [{"id": str(i), "file": f"papers/{i}.md", "tokens": 70_000 if i == 0 else 8_000} for i in range(7)]
    quick = plan_reading(many, PROFILES[ResearchDepth.quick])  # 45k budget, 3 papers
    assert quick[0]["mode"] == "full" and all(e["mode"] == "skipped" for e in quick[1:])


async def test_research_resumes_and_resets(settings):
    claude = FakeClaude(default_responses())
    job_dir = job(settings, ResearchDepth.quick)
    ctx = ctx_for(settings, job_dir, claude)
    await run_pipeline(ctx)

    # Simulate a crash during reading: only the final notes are missing.
    (job_dir / "research.md").unlink()
    claude.responses["ResearchResult"] = [default_responses()["ResearchResult"][0]]
    await run_pipeline(ctx)
    assert len(claude.calls_for("ScoutResult")) == 2 and len(claude.calls_for("ResearchResult")) == 2
    assert len(claude.calls_for("ResearchPlan")) == 1  # the plan is reused on resume

    # Forcing the research stage starts from scratch.
    fresh = default_responses()
    claude.responses.update({k: fresh[k] for k in ("ScoutResult", "Selection", "ResearchResult", "Script")})
    await run_pipeline(ctx, from_stage="research")
    assert len(claude.calls_for("ScoutResult")) == 4 and len(claude.calls_for("Selection")) == 2
    assert len(claude.calls_for("ResearchPlan")) == 2


async def test_all_scouts_failing_is_an_error(settings):
    def boom(call):
        raise RuntimeError("down")

    responses = default_responses()
    responses["ScoutResult"] = [boom] * 3
    ctx = ctx_for(settings, job(settings), FakeClaude(responses))
    with pytest.raises(PodcastError, match="alle 3 Scouts"):
        await run_pipeline(ctx)


async def test_selection_with_only_unknown_ids_falls_back_to_ranking(settings):
    responses = default_responses()
    responses["Selection"] = [{"focus": "F", "selected": [{"id": "nope", "reason": "x"}]}]
    job_dir = job(settings, ResearchDepth.quick)
    await run_pipeline(ctx_for(settings, job_dir, FakeClaude(responses)))
    selection = json.loads((job_dir / "selection.json").read_text())
    assert [s["id"] for s in selection["selected"]] == ["arxiv:2401.00001", "doi:10.1000/xyz"]


async def test_script_without_full_texts_has_no_lookup_section(settings):
    job_dir = job(settings, ResearchDepth.quick)
    claude = FakeClaude(default_responses())
    await run_pipeline(ctx_for(settings, job_dir, claude, routes={}))  # every download fails
    (script_call,) = claude.calls_for("Script")
    assert "Volltexte zum Nachschlagen" not in script_call.prompt
    assert "und die Volltexte" not in script_call.prompt


async def test_scout_failure_names_the_cause(settings):
    from app.claude import claude_error
    from app.pipeline.research import scouts_failed

    auth = claude_error("ProcessError: exit code 1", ["Invalid API key · Please run /login"])
    error = scouts_failed({"background": auth, "core": auth, "critique": auth})
    assert error.message.startswith("Die Recherche ist fehlgeschlagen (alle 3 Scouts). Claude konnte sich nicht anmelden")
    assert "Scout 'core'" in error.details and "Invalid API key" in error.details

    mixed = scouts_failed({"core": auth, "critique": TimeoutError("slow")})
    assert "aus verschiedenen Gründen" in mixed.message and "slow" in mixed.message and "anmelden" in mixed.message
    assert "TimeoutError: slow" in mixed.details


def test_claude_error_classification():
    from app.claude import claude_error

    assert "Nutzungslimit" in claude_error("API Error: 429 usage limit reached").message
    assert "überlastet" in claude_error("", ["API Error: 529 Overloaded"]).message
    assert "Netzwerk" in claude_error("Error: getaddrinfo ENOTFOUND api.anthropic.com").message
    assert "Rundenlimit" in claude_error("Claude failed: subtype=error_max_turns").message
    generic = claude_error("Something odd about arXiv 2401.00001")  # no false 401 match
    assert generic.message == "Claude ist mit einem Fehler abgebrochen." and "2401.00001" in generic.details


async def test_title_is_set_as_soon_as_research_names_it(settings):
    titles = []
    responses = default_responses()
    job_dir = job(settings, ResearchDepth.quick)
    ctx = ctx_for(settings, job_dir, FakeClaude(responses))
    ctx.on_title = titles.append
    await run_pipeline(ctx)
    assert titles == ["Testthema", "Testepisode"]  # research title first, then the script's


def test_short_papers_fill_the_budget():
    from app.pipeline.research import PROFILES, plan_reading

    short = [{"id": str(i), "file": f"papers/{i}.md", "tokens": 9_000} for i in range(10)]
    deep = plan_reading(short, PROFILES[ResearchDepth.deep])  # 130k budget, at most 7 papers
    assert [e["mode"] for e in deep].count("full") == 7  # short papers: the paper limit applies first
    long = [{"id": str(i), "file": f"papers/{i}.md", "tokens": 35_000} for i in range(10)]
    modes = [e["mode"] for e in plan_reading(long, PROFILES[ResearchDepth.deep])]
    assert modes.count("full") == 3 and modes.count("selective") == 1  # long papers: the budget applies first


# --- main text, references, appendix ---------------------------------------------


def test_split_paper_drops_references_and_keeps_appendix_separate():
    from app.papers import split_paper

    body = "## 1 Introduction\n\n" + "Main text. " * 300 + "\n\n## 6 Conclusion\n\nWe conclude.\n\n"
    main, appendix, refs = split_paper(body + "## References\n\n[1] A. 2020.\n\n## A Proofs\n\nProof.\n")
    assert main.endswith("We conclude.") and appendix.startswith("## A Proofs") and refs
    main, appendix, refs = split_paper(body + "## Appendix A Details\n\nX\n\n## References\n\n[1] Z")
    assert main.endswith("We conclude.") and appendix == "## Appendix A Details\n\nX" and refs
    # PDF text: plain paragraphs; a sentence that mentions the appendix is not a heading.
    pdf = "Body. " * 300 + "\n\nAppendix B lists all runs.\n\nMore body.\n\nREFERENCES\n\n[1] Foo."
    main, appendix, refs = split_paper(pdf)
    assert main.endswith("More body.") and appendix == "" and refs
    toc = "Contents\n\nReferences\n\n" + "Body. " * 300  # an early 'References' is a table of contents
    assert split_paper(toc) == (toc.rstrip(), "", False)


async def test_long_paper_keeps_main_text_and_moves_appendix(tmp_path):
    from app.papers import PaperFile, coverage_note, save_paper
    from dataclasses import asdict

    text = ("## 1 Intro\n\n" + "Main. " * 2000 + "\n\n## References\n\n" + "[1] Ref.\n\n" * 500
            + "## A Extra results\n\n" + "Appendix. " * 300)
    paper = PaperFile(id="arxiv:1", title="T")
    save_paper(paper, "T", text, "https://x.org", tmp_path, max_chars=50_000)
    main = (tmp_path / "arxiv_1.md").read_text()
    assert not paper.truncated and "[1] Ref." not in main and "Appendix." not in main  # main part complete
    assert paper.appendix_file == "papers/arxiv_1.appendix.md" and paper.references_removed
    assert "The appendix is in a separate file `papers/arxiv_1.appendix.md`" in main
    assert "Appendix." in (tmp_path / "arxiv_1.appendix.md").read_text()

    short = PaperFile(id="arxiv:2", title="T")
    save_paper(short, "T", "## 1 Intro\n\n" + "Main text here. " * 3000, "https://x.org", tmp_path, max_chars=20_000)
    assert short.truncated and short.omitted_chars > 0
    assert "TRUNCATED" in (tmp_path / "arxiv_2.md").read_text()
    note = coverage_note(asdict(short), "selective")
    assert "main text was cut" in note and "only its key sections were read" in note


async def test_later_agents_are_told_what_they_do_not_see(settings):
    long_html = ARXIV_HTML.replace("</article>", "<section class='ltx_appendix'><h2>Appendix A Extra</h2>"
                                   "<p>" + "Appendix text. " * 200 + "</p></section></article>")
    routes = {"https://arxiv.org/html/2401.00001": (200, "text/html", long_html.encode())}
    claude = FakeClaude(default_responses())
    job_dir = job(settings, ResearchDepth.quick)
    await run_pipeline(ctx_for(settings, job_dir, claude, routes=routes))
    index = json.loads((job_dir / "papers/index.json").read_text())
    assert index[0]["appendix_file"] == "papers/arxiv_2401.00001.appendix.md"
    (read,) = claude.calls_for("ResearchResult")
    assert "Coverage: The appendix is in a separate file `papers/arxiv_2401.00001.appendix.md`" in read.prompt
    assert "Say what you did not read" in read.prompt
    (script,) = claude.calls_for("Script")
    assert "papers/arxiv_2401.00001.appendix.md" in script.prompt
    assert "During the research its complete main text was read." in script.prompt


async def test_plan_with_too_few_scouts_is_retried_then_filled(settings):
    from tests.conftest import PLAN

    one_task = {**PLAN, "tasks": PLAN["tasks"][:1]}
    responses = default_responses()
    responses["ResearchPlan"] = [one_task, one_task]  # asked twice, still one task
    responses["ScoutResult"] = [default_responses()["ScoutResult"][0]] * 5
    claude = FakeClaude(responses)
    job_dir = job(settings, ResearchDepth.deep)  # needs at least 3 scouts
    await run_pipeline(ctx_for(settings, job_dir, claude))
    retry = claude.calls_for("ResearchPlan")[1]
    assert retry.resume and "needs between 3 and 6" in retry.prompt
    plan = json.loads((job_dir / "plan.json").read_text())
    assert [t["title"] for t in plan["tasks"]] == ["Core work", "Critique and replications", "Foundations"]
    assert len(claude.calls_for("ScoutResult")) == 3


async def test_plan_with_too_few_key_questions_is_retried_then_filled(settings):
    from tests.conftest import PLAN

    thin = {**PLAN, "key_questions": ["Was zeigt das Paper?"]}
    responses = default_responses()
    responses["ResearchPlan"] = [thin, thin]
    claude = FakeClaude(responses)
    job_dir = job(settings, ResearchDepth.quick)
    await run_pipeline(ctx_for(settings, job_dir, claude))
    retry = claude.calls_for("ResearchPlan")[1]
    assert retry.resume and "1 key question(s); it needs 3 to 8" in retry.prompt
    questions = json.loads((job_dir / "plan.json").read_text())["key_questions"]
    assert questions[0] == "Was zeigt das Paper?" and len(questions) == 3
