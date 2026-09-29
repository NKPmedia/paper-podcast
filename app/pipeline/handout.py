"""Stage (optional): handout PDF with Claude-written plots and rendered formulas.

Runs after the script so it can cover everything the conversation points to
(``handout_items``). Claude returns Markdown plus plot code and LaTeX formulas; our
code renders them (``app.plots``) and builds the PDF with WeasyPrint.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from markdown_it import MarkdownIt

from app.claude import ClaudeCall
from app.models import Handout, PlotFixes, Script, Source, json_schema
from app.plots import render_formula, render_plot
from app.prompts import render_stage

NAME = "handout"
DESCRIPTION = "Claude erstellt das Handout"

TOOLS = ["Read"]
PLACEHOLDER = re.compile(r"<p>\{\{(plot|formula):([a-z0-9-]+)\}\}</p>")
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")

CSS = """
@page { size: A4; margin: 20mm 18mm 22mm;
  @bottom-center { content: counter(page) " / " counter(pages); font: 8.5pt "DejaVu Sans", sans-serif; color: #777; }
  @top-right { content: string(podcast); font: 8.5pt "DejaVu Sans", sans-serif; color: #777; } }
body { font-family: "DejaVu Sans", sans-serif; font-size: 10pt; line-height: 1.45; color: #1d1d1f; }
.kicker { string-set: podcast content(); color: #2f6db5; font-weight: bold; font-size: 9pt;
  text-transform: uppercase; letter-spacing: 0.06em; margin: 0; }
.date { color: #777; font-size: 9pt; margin: 2pt 0 12pt; }
h1 { font-size: 20pt; line-height: 1.2; margin: 4pt 0 2pt; }
h2 { font-size: 13pt; margin: 16pt 0 4pt; color: #2f6db5; }
h3 { font-size: 11pt; margin: 12pt 0 3pt; }
p, li { orphans: 3; widows: 3; }
a { color: #2f6db5; text-decoration: none; word-break: break-all; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 9pt; }
th, td { border-bottom: 0.5pt solid #ccc; padding: 3pt 5pt; text-align: left; vertical-align: top; }
figure { margin: 10pt 0; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; }
figure.formula img { max-width: 90%; max-height: 28mm; }
figcaption { font-size: 8.5pt; color: #555; margin-top: 3pt; }
pre.formula-src { background: #f3f3f3; padding: 6pt; font-size: 9pt; white-space: pre-wrap; }
"""


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
    index = ctx.path("papers/index.json")
    papers = [p for p in json.loads(index.read_text(encoding="utf-8")) if p["file"]] if index.exists() else []

    prompt = render_stage(
        "handout", blocks=ctx.blocks, script=script, sources=sources, papers=papers,
        notes=ctx.path("research.md").read_text(encoding="utf-8"),
    )
    model = ctx.settings.script_model
    call = ClaudeCall(
        prompt=prompt, cwd=ctx.job_dir, tools=TOOLS, skills=skills, system_append=ctx.blocks["system"],
        output_schema=json_schema(Handout), max_turns=ctx.settings.claude_max_turns_script, model=model,
    )
    result = await ctx.claude.run(call)
    handout = Handout.model_validate(result.structured)
    ctx.log.write("claude", stage=NAME, model=model, cost_usd=result.cost_usd, turns=result.num_turns,
                  skills_used=result.skills_used, plots=len(handout.plots), formulas=len(handout.formulas))

    out = ctx.path("handout")
    shutil.rmtree(out, ignore_errors=True)
    figures = out / "figures"
    for plot in handout.plots:
        plot.name = _safe_name(plot.name)
    errors = {p.name: render_plot(p.code, figures / f"{p.name}.png").error for p in handout.plots}
    errors = {name: error for name, error in errors.items() if error}

    if errors:  # one repair round: Claude sees the errors and fixes only the broken plots
        await ctx.notify(NAME, f"{len(errors)} Abbildung(en) werden repariert")
        fix_prompt = "Diese Abbildungen konnten nicht erzeugt werden:\n\n" + "\n\n".join(
            f"## {name}\n```\n{error}\n```" for name, error in errors.items()
        ) + "\n\nKorrigiere nur diese Abbildungen (gleiche Namen) und beachte die Regeln für den Plot-Code."
        fix = await ctx.claude.run(ClaudeCall(
            prompt=fix_prompt, cwd=ctx.job_dir, tools=TOOLS, skills=skills, system_append=ctx.blocks["system"],
            output_schema=json_schema(PlotFixes), max_turns=10, model=model, resume=result.session_id,
        ))
        ctx.log.write("claude", stage=NAME, step="plot-fix", model=model, cost_usd=fix.cost_usd,
                      turns=fix.num_turns, skills_used=fix.skills_used)
        by_name = {p.name: p for p in handout.plots}
        for plot in PlotFixes.model_validate(fix.structured).plots:
            plot.name = _safe_name(plot.name)
            if plot.name in errors:
                by_name[plot.name].code = plot.code
                by_name[plot.name].caption = plot.caption or by_name[plot.name].caption
                outcome = render_plot(plot.code, figures / f"{plot.name}.png")
                if outcome.ok:
                    del errors[plot.name]
                else:
                    errors[plot.name] = outcome.error

    formula_errors = {}
    for formula in handout.formulas:
        formula.name = _safe_name(formula.name)
        outcome = render_formula(formula.latex, figures / f"formula-{formula.name}.png")
        if not outcome.ok:
            formula_errors[formula.name] = outcome.error
    ctx.log.write("handout", plot_errors=errors, formula_errors=list(formula_errors))

    (out / "handout.md").write_text(handout.markdown, encoding="utf-8")
    (out / "handout.json").write_text(handout.model_dump_json(indent=2), encoding="utf-8")
    html_doc = build_html(ctx.settings.podcast_name, script.title, handout, figures, set(errors), set(formula_errors))
    (out / "handout.html").write_text(html_doc, encoding="utf-8")
    write_pdf(html_doc, out, ctx.path("handout.pdf"))


def build_html(podcast: str, title: str, handout: Handout, figures: Path, failed_plots: set, failed_formulas: set) -> str:
    plots = {p.name: p for p in handout.plots}
    formulas = {f.name: f for f in handout.formulas}
    used: set[str] = set()

    def figure_html(kind: str, name: str) -> str:
        if kind == "plot" and name in plots and name not in failed_plots:
            used.add(name)
            return (f'<figure><img src="figures/{name}.png" alt="">'
                    f"<figcaption>{html.escape(plots[name].caption)}</figcaption></figure>")
        if kind == "formula" and name in formulas:
            if name in failed_formulas:  # show the source instead of dropping it
                return f'<pre class="formula-src">{html.escape(formulas[name].latex)}</pre>'
            return f'<figure class="formula"><img src="figures/formula-{name}.png" alt=""></figure>'
        return ""

    body = PLACEHOLDER.sub(lambda m: figure_html(m.group(1), m.group(2)), _markdown.render(handout.markdown))
    leftovers = [n for n in plots if n not in used and n not in failed_plots]
    if leftovers:
        body += "<h2>Abbildungen</h2>" + "".join(figure_html("plot", n) for n in leftovers)
    date = datetime.now().strftime("%d.%m.%Y")
    return (
        f'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f"<style>{CSS}</style></head><body>"
        f'<p class="kicker">{html.escape(podcast)} · Handout</p><p class="date">{date}</p>{body}</body></html>'
    )


def write_pdf(html_doc: str, base: Path, target: Path) -> None:
    from urllib.parse import unquote, urlparse

    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    base = base.resolve()

    class LocalOnlyFetcher(URLFetcher):
        """Only files inside the handout directory: no network, no other files."""

        def fetch(self, url, headers=None):
            path = Path(unquote(urlparse(url).path)).resolve()
            if not path.is_relative_to(base):
                raise ValueError(f"blocked resource: {url}")
            return super().fetch(url, headers)

    tmp = target.with_suffix(".part")
    fetcher = LocalOnlyFetcher(allowed_protocols={"file"}, allow_redirects=False)
    HTML(string=html_doc, base_url=str(base) + "/", url_fetcher=fetcher).write_pdf(tmp)
    tmp.rename(target)
