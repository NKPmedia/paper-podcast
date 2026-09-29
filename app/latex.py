"""Build the handout PDF with LaTeX.

Claude writes only the document *body*; the preamble (packages, layout, header) is
ours. The body is untrusted (it is shaped by web content), so:

1. ``check_body`` rejects commands that read or write files, define macros, load
   packages, run external programs, or smuggle characters (``^^`` escapes).
2. ``pdflatex`` runs with ``-no-shell-escape``, kpathsea's paranoid file access
   (``openin_any=p``/``openout_any=p``: no absolute paths, no ``..``), in a temporary
   directory holding only the .tex file and the figures, with an empty environment,
   a timeout and resource limits.

Figures are included only through ``\\plot{name}``, which we expand ourselves.
"""

from __future__ import annotations

import os
import re
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 120

FORBIDDEN = re.compile(
    r"\\(input|include|includeonly|InputIfFileExists|IfFileExists|openin|openout|read|readline|write|"
    r"immediate|message|closein|closeout|newread|newwrite|catcode|lccode|uccode|mathcode|sfcode|delcode|"
    r"usepackage|RequirePackage|documentclass|def|edef|gdef|xdef|let|futurelet|expandafter|csname|"
    r"newcommand|renewcommand|providecommand|DeclareRobustCommand|newenvironment|renewenvironment|"
    r"special|pdfliteral|pdfobj|pdfximage|directlua|latelua|write18|ShellEscape|jobname|makeatletter|"
    r"makeatother|verbatiminput|lstinputlisting|includegraphics|includepdf|pdffilesize|pdfmdfivesum|"
    r"pdffiledump|filemoddate|endinput|scantokens|everyjob|everypar|output|shipout|end\s*\{document\}|"
    r"begin\s*\{document\})(?![A-Za-z])"
)
PLOT_MACRO = re.compile(r"\\plot\{([a-z0-9-]+)\}")

PREAMBLE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[ngerman]{babel}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage[a4paper,top=2.3cm,bottom=2.5cm,left=2.2cm,right=2.2cm,headheight=14pt]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{tabularx}
\usepackage{array}
\usepackage{enumitem}
\usepackage[dvipsnames]{xcolor}
\usepackage{caption}
\usepackage{float}
\usepackage{fancyhdr}
\usepackage{lastpage}
\usepackage{url}
\definecolor{accent}{HTML}{2F6DB5}
\usepackage[colorlinks=true,linkcolor=accent,urlcolor=accent,citecolor=accent,
  pdftitle={<<TITLE_PDF>>},pdfauthor={<<PODCAST_PDF>>}]{hyperref}
\captionsetup{font=small,labelfont={bf,color=accent}}
\setlist{itemsep=2pt,topsep=4pt}
\setlength{\parindent}{0pt}
\setlength{\parskip}{5pt plus 2pt}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[R]{\small\color{gray}<<PODCAST>> · Handout}
\fancyfoot[C]{\small\color{gray}\thepage\ /\ \pageref*{LastPage}}
\renewcommand{\headrulewidth}{0pt}
\fancypagestyle{plain}{\fancyhf{}\fancyfoot[C]{\small\color{gray}\thepage\ /\ \pageref*{LastPage}}}
\usepackage{titlesec}
\titleformat{\section}{\large\bfseries\color{accent}}{}{0pt}{}
\titleformat{\subsection}{\normalsize\bfseries}{}{0pt}{}
\titlespacing*{\section}{0pt}{14pt}{4pt}
\titlespacing*{\subsection}{0pt}{10pt}{2pt}
\begin{document}
\thispagestyle{plain}
{\small\color{accent}\textbf{\MakeUppercase{<<PODCAST>> · Handout}}}\par
{\small\color{gray}<<DATE>>}\par\vspace{4pt}
{\LARGE\bfseries <<TITLE>>\par}
\vspace{8pt}
"""

POSTAMBLE = "\n\\end{document}\n"

_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


class LatexRejected(ValueError):
    pass


@dataclass
class CompileResult:
    ok: bool
    error: str = ""


def escape_text(text: str) -> str:
    """Escape plain text (titles, names) for LaTeX."""
    return "".join(_LATEX_SPECIAL.get(ch, ch) for ch in text)


def check_body(body: str) -> None:
    if "^^" in body:
        raise LatexRejected("„^^“-Zeichenfolgen sind nicht erlaubt")
    match = FORBIDDEN.search(body)
    if match:
        raise LatexRejected(f"Befehl nicht erlaubt: {match.group(0)}")


def expand_plots(body: str, plots: dict[str, str], available: set[str]) -> str:
    """Replace ``\\plot{name}`` with a figure; unknown or failed plots are dropped.

    ``plots`` maps names to LaTeX captions. Plots that exist but are not referenced
    are appended at the end.
    """
    used = set()

    def figure(name: str) -> str:
        used.add(name)
        return (
            "\\begin{figure}[H]\n\\centering\n"
            f"\\includegraphics[width=0.92\\linewidth]{{figures/{name}.pdf}}\n"
            f"\\caption{{{plots[name]}}}\n\\end{{figure}}"
        )

    body = PLOT_MACRO.sub(lambda m: figure(m.group(1)) if m.group(1) in available and m.group(1) in plots else "", body)
    leftovers = [n for n in plots if n in available and n not in used]
    if leftovers:
        body += "\n\n\\section{Abbildungen}\n" + "\n\n".join(figure(n) for n in leftovers)
    return body


def build_document(podcast: str, title: str, date: str, body: str) -> str:
    pdf_safe = lambda s: re.sub(r"[{}\\%#&$^_~]", "", s)  # noqa: E731
    head = (
        PREAMBLE.replace("<<PODCAST_PDF>>", pdf_safe(podcast)).replace("<<TITLE_PDF>>", pdf_safe(title))
        .replace("<<PODCAST>>", escape_text(podcast)).replace("<<TITLE>>", escape_text(title))
        .replace("<<DATE>>", escape_text(date))
    )
    return head + body.strip() + POSTAMBLE


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024**2, 50 * 1024**2))


def error_excerpt(log: str, source: str) -> str:
    """The first LaTeX error with context and the offending source line."""
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("!") or re.match(r"^\./handout\.tex:\d+:", line):
            excerpt = "\n".join(lines[i:i + 6])
            m = re.search(r"handout\.tex:(\d+):", line) or re.search(r"^l\.(\d+)", "\n".join(lines[i:i + 12]), re.M)
            if m:
                number = int(m.group(1))
                src = source.splitlines()
                if 0 < number <= len(src):
                    excerpt += f"\n\nZeile {number} im Dokument: {src[number - 1][:300]}"
            return excerpt[:2500]
    return "\n".join(lines[-15:])[:2500] or "pdflatex ist ohne Log fehlgeschlagen"


def compile_pdf(tex: str, figures: Path, target: Path) -> CompileResult:
    pdflatex = shutil.which("pdflatex")
    if not pdflatex:
        return CompileResult(False, "pdflatex ist nicht installiert (TeX Live fehlt im Container)")
    with tempfile.TemporaryDirectory(prefix="latex-") as tmp:
        work = Path(tmp)
        (work / "handout.tex").write_text(tex, encoding="utf-8")
        if figures.is_dir():
            shutil.copytree(figures, work / "figures")
        env = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": tmp, "TEXMFOUTPUT": tmp,
            "TEXMFVAR": str(work / ".texmf-var"), "openin_any": "p", "openout_any": "p", "shell_escape": "f",
        }
        cmd = [pdflatex, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
               "-file-line-error", "handout.tex"]
        for _ in range(2):  # the second run resolves the page count (LastPage)
            try:
                proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, timeout=TIMEOUT_S,
                                      preexec_fn=_limits)
            except subprocess.TimeoutExpired:
                return CompileResult(False, f"pdflatex hat das Zeitlimit von {TIMEOUT_S} s überschritten")
            log = proc.stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0 or not (work / "handout.pdf").exists():
                return CompileResult(False, error_excerpt(log, tex))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / "handout.pdf", target)
    return CompileResult(True)
