"""Run Claude-written matplotlib code for the handout, as safely as practical.

The code is influenced by web content, so it is treated as untrusted:

1. Static check (AST): only ``matplotlib``, ``numpy`` and ``math`` may be imported;
   no dunder attributes, no builtins that reach files or code (``open``, ``exec``,
   ``__import__`` ...), no file-writing or file-reading methods.
2. It runs in a separate Python process (``-I``: isolated mode) with an empty
   environment (no secrets), a temporary working directory and CPU, memory and
   file-size limits.
3. The runner, not the snippet, saves the current figure to a PNG.

Figures are saved as vector PDFs for the LaTeX handout.
"""

from __future__ import annotations

import ast
import json
import os
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ALLOWED_MODULES = {"matplotlib", "numpy", "math"}
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "breakpoint", "exit", "quit", "help", "memoryview", "type", "object", "super",
}
FORBIDDEN_ATTRS = {
    "savefig", "show", "save", "savez", "savez_compressed", "savetxt", "load", "loadtxt", "genfromtxt",
    "fromfile", "tofile", "memmap", "DataSource", "imread", "imsave", "use", "rc_file", "get_cachedir",
    "switch_backend", "get_configdir", "system", "popen",
}
TIMEOUT_S = 90

RUNNER = r'''
import json, math, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

job = json.load(open(sys.argv[1]))
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                     "figure.figsize": (6.5, 3.8), "pdf.fonttype": 42,
                     "font.family": "serif", "font.serif": ["Latin Modern Roman", "CMU Serif", "DejaVu Serif"],
                     "mathtext.fontset": "cm", "axes.unicode_minus": False})
exec(compile(job["code"], "<plot>", "exec"), {"plt": plt, "np": np, "math": math, "matplotlib": matplotlib})
fig = plt.gcf()
if not fig.axes:
    raise SystemExit("Der Code hat keine Abbildung gezeichnet.")
fig.savefig(job["out"], bbox_inches="tight")
'''


class PlotRejected(ValueError):
    pass


@dataclass
class RenderResult:
    ok: bool
    error: str = ""


def check_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise PlotRejected(f"Syntax error: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                if name.split(".")[0] not in ALLOWED_MODULES:
                    raise PlotRejected(f"Import not allowed: {name} (only matplotlib, numpy, math)")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise PlotRejected(f"Not allowed: {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise PlotRejected(f"Not allowed: .{node.attr}")
            if node.attr in FORBIDDEN_ATTRS:
                raise PlotRejected(f"Not allowed: .{node.attr}() – the runner saves the figure itself")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise PlotRejected("global/nonlocal is not allowed")


def _limits() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20 * 1024**2, 20 * 1024**2))


def _run(job: dict) -> RenderResult:
    with tempfile.TemporaryDirectory(prefix="plot-") as tmp:
        job_file = Path(tmp) / "job.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        env = {"MPLBACKEND": "Agg", "MPLCONFIGDIR": tmp, "HOME": tmp, "PATH": os.defpath}
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", RUNNER, str(job_file)],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=TIMEOUT_S, preexec_fn=_limits,
            )
        except subprocess.TimeoutExpired:
            return RenderResult(False, f"Zeitlimit von {TIMEOUT_S} s überschritten")
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        return RenderResult(False, "\n".join(lines[-8:])[-1500:] or f"Exit-Code {proc.returncode}")
    if not Path(job["out"]).exists():
        return RenderResult(False, "Keine Ausgabe erzeugt")
    return RenderResult(True)


def render_plot(code: str, out: Path) -> RenderResult:
    try:
        check_code(code)
    except PlotRejected as exc:
        return RenderResult(False, str(exc))
    out.parent.mkdir(parents=True, exist_ok=True)
    return _run({"code": code, "out": str(out.resolve())})
