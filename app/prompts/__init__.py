"""Prompt blocks: user-editable pieces of the prompts sent to Claude.

Defaults ship in ``app/prompts/defaults/<block>.md``. A file with the same name in
``<data>/prompts/`` overrides the default. Blocks are Jinja2 templates rendered with
the episode context (names, target length, ...).

Stage templates in ``app/prompts/stages/`` stitch the blocks together; they are
part of the code, not user-editable.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.config import APP_DIR

DEFAULTS_DIR = APP_DIR / "prompts" / "defaults"
STAGES_DIR = APP_DIR / "prompts" / "stages"

BLOCK_DESCRIPTIONS = {
    "system": "Globale Regeln (Sprache, Quellen, keine erfundenen Zahlen)",
    "personas": "Namen, Hintergrund und Sprechweise von Host und Expert*in",
    "style": "Ton, Humor, Anrede, Zielgruppe",
    "structure": "Aufbau der Episode: Intro, Kapitel, Zusammenfassung, Outro",
    "research": "Wie recherchiert wird und welche Quellen bevorzugt werden",
    "script_rules": "Regeln für gesprochene Sprache und TTS",
    "handout": "Aufbau des Handouts und Hinweise für Plots",
}

_env = Environment(
    loader=FileSystemLoader(str(STAGES_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


class PromptStore:
    def __init__(self, override_dir: Path):
        self.override_dir = override_dir

    def names(self) -> list[str]:
        return sorted(p.stem for p in DEFAULTS_DIR.glob("*.md"))

    def default(self, name: str) -> str:
        return (DEFAULTS_DIR / f"{name}.md").read_text(encoding="utf-8")

    def is_overridden(self, name: str) -> bool:
        return (self.override_dir / f"{name}.md").exists()

    def raw(self, name: str) -> str:
        override = self.override_dir / f"{name}.md"
        if override.exists():
            return override.read_text(encoding="utf-8")
        return self.default(name)

    def save(self, name: str, text: str) -> None:
        if name not in self.names():
            raise KeyError(f"Unknown prompt block: {name}")
        self.override_dir.mkdir(parents=True, exist_ok=True)
        (self.override_dir / f"{name}.md").write_text(text, encoding="utf-8")

    def reset(self, name: str) -> None:
        (self.override_dir / f"{name}.md").unlink(missing_ok=True)

    def resolve(self, context: dict, block_overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Render every block with ``context``; per-request additions are appended."""
        block_overrides = block_overrides or {}
        unknown = set(block_overrides) - set(self.names())
        if unknown:
            raise KeyError(f"Unknown prompt block(s): {', '.join(sorted(unknown))}")
        blocks = {}
        for name in self.names():
            text = Environment(undefined=StrictUndefined).from_string(self.raw(name)).render(**context)
            extra = block_overrides.get(name, "").strip()
            if extra:
                text = f"{text.rstrip()}\n\nZusätzlich für diese Episode:\n{extra}\n"
            blocks[name] = text.strip()
        return blocks


def render_stage(stage: str, **context) -> str:
    return _env.get_template(f"{stage}.md.j2").render(**context)
