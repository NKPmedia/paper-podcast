"""Stage (optional): handout PDF, typeset with LaTeX.

Runs after the script so it can cover everything the conversation points to
(``handout_items``). Claude writes the LaTeX body and matplotlib code; our code
renders the plots (``app.plots``), checks the body and compiles it (``app.latex``).
Broken plots and LaTeX errors get repair rounds in the same Claude session.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime

from app.claude import ClaudeCall
from app.latex import LatexRejected, build_document, check_body, compile_pdf, expand_plots
from app.models import Handout, HandoutFix, PlotFixes, Script, Source, json_schema
from app.plots import render_plot
from app.errors import PodcastError
from app.pipeline.research import papers_for_prompt
from app.prompts import render_stage

NAME = "handout"
DESCRIPTION = "Claude erstellt das Handout"

TOOLS = ["Read"]
LATEX_REPAIRS = 2


def enabled(ctx) -> bool:
    return ctx.request.options.handout


def is_done(ctx) -> bool:
    return ctx.path("handout.pdf").exists()


def reset(ctx) -> None:
    shutil.rmtree(ctx.path("handout"), ignore_errors=True)
    ctx.path("handout.pdf").unlink(missing_ok=True)


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "abbildung"


async def run(ctx) -> None:
    opts = ctx.request.options
    skills = ctx.skills.stage_skills(NAME, opts.extra_skills)
    ctx.skills.install(ctx.job_dir, skills)
    script = Script.model_validate_json(ctx.path("script.json").read_text(encoding="utf-8"))
    sources = [Source(**s) for s in json.loads(ctx.path("sources.json").read_text(encoding="utf-8"))]
    papers = papers_for_prompt(ctx.job_dir)  # with notes on cut text, appendix and reading depth
    model = ctx.settings.script_model

    async def ask(prompt: str, schema, resume: str | None = None, step: str = "write", max_turns=None):
        result = await ctx.claude.run(ClaudeCall(
            prompt=prompt, cwd=ctx.job_dir, tools=TOOLS, skills=skills, system_append=ctx.blocks["system"],
            output_schema=json_schema(schema), max_turns=max_turns or ctx.settings.claude_max_turns_script,
            model=model, resume=resume,
        ))
        ctx.log_claude(NAME, model, result, step=step)
        return result

    prompt = render_stage(
        "handout", blocks=ctx.blocks, script=script, sources=sources, papers=papers,
        notes=ctx.path("research.md").read_text(encoding="utf-8"),
        language=opts.language.value, language_name=opts.language.english_name,
    )
    result = await ask(prompt, Handout)
    session = result.session_id
    handout = Handout.model_validate(result.structured)

    out = ctx.path("handout")
    shutil.rmtree(out, ignore_errors=True)
    figures = out / "figures"

    # --- plots (one repair round) ---
    for plot in handout.plots:
        plot.name = _safe_name(plot.name)
    errors = {p.name: render_plot(p.code, figures / f"{p.name}.pdf").error for p in handout.plots}
    errors = {name: error for name, error in errors.items() if error}
    if errors:
        await ctx.notify(NAME, f"{len(errors)} Abbildung(en) werden repariert")
        fix_prompt = "These figures could not be rendered:\n\n" + "\n\n".join(
            f"## {name}\n```\n{error}\n```" for name, error in errors.items()
        ) + "\n\nFix only these figures (same names) and follow the rules for plot code."
        fix = await ask(fix_prompt, PlotFixes, resume=session, step="plot-fix", max_turns=10)
        session = fix.session_id
        by_name = {p.name: p for p in handout.plots}
        for plot in PlotFixes.model_validate(fix.structured).plots:
            plot.name = _safe_name(plot.name)
            if plot.name in errors:
                by_name[plot.name].code = plot.code
                by_name[plot.name].caption = plot.caption or by_name[plot.name].caption
                outcome = render_plot(plot.code, figures / f"{plot.name}.pdf")
                if outcome.ok:
                    del errors[plot.name]
                else:
                    errors[plot.name] = outcome.error

    # --- LaTeX: check and compile; repair rounds get the error log ---
    available = {p.name for p in handout.plots if p.name not in errors}
    captions = {p.name: p.caption for p in handout.plots}
    english = opts.language.value == "en"
    date = datetime.now().strftime("%B %-d, %Y" if english else "%d.%m.%Y")
    body = handout.latex_body
    latex_errors: list[str] = []
    part = ctx.path("handout.pdf.part")
    for attempt in range(LATEX_REPAIRS + 1):
        tex = ""
        try:
            check_body(body)
            for caption in captions.values():
                check_body(caption)
            tex = build_document(ctx.settings.podcast_name, script.title, date,
                                 expand_plots(body, captions, available, opts.language.value),
                                 opts.language.value)
            outcome = compile_pdf(tex, figures, part)
            error = "" if outcome.ok else outcome.error
        except LatexRejected as exc:
            error = f"Security check: {exc}"
        if not error:
            break
        latex_errors.append(error)
        if "nicht installiert" in error:
            raise PodcastError("Das Handout kann nicht gesetzt werden: pdflatex fehlt im Container.", error)
        if attempt == LATEX_REPAIRS:
            raise PodcastError(
                f"Das Handout ließ sich auch nach {LATEX_REPAIRS} Korrekturen nicht mit LaTeX setzen. Mit "
                "„Fortsetzen“ erneut versuchen oder die Episode ohne Handout neu erzeugen.",
                "\n\n---\n\n".join(latex_errors),
            )
        await ctx.notify(NAME, "LaTeX-Fehler im Handout wird korrigiert")
        fix = await ask(
            "The handout failed to compile with pdflatex:\n\n```\n" + error + "\n```\n\n"
            "Fix the LaTeX document body and return it in full. Follow the rules of the "
            "`latex-handout` skill: no preamble, no custom commands, no packages, figures only via "
            "\\plot{name}, escape special characters such as & % $ # _.",
            HandoutFix, resume=session, step=f"latex-fix-{attempt + 1}", max_turns=10,
        )
        session = fix.session_id
        body = HandoutFix.model_validate(fix.structured).latex_body

    out.mkdir(parents=True, exist_ok=True)
    (out / "handout.tex").write_text(tex, encoding="utf-8")
    (out / "handout.json").write_text(
        json.dumps({"latex_body": body, "plots": [p.model_dump() for p in handout.plots]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ctx.log.write("handout", plot_errors=errors, latex_errors=[e[:500] for e in latex_errors])
    part.rename(ctx.path("handout.pdf"))
