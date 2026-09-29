"""Data models shared by the pipeline stages.

The pydantic models double as JSON schemas for Claude's structured output.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Length(str, Enum):
    kurz = "kurz"
    mittel = "mittel"
    lang = "lang"

    @property
    def minutes(self) -> int:
        return {"kurz": 5, "mittel": 12, "lang": 25}[self.value]


class ResearchDepth(str, Enum):
    quick = "quick"
    medium = "medium"
    deep = "deep"


class EpisodeOptions(BaseModel):
    length: Length = Length.mittel
    research_depth: ResearchDepth = ResearchDepth.medium
    handout: bool = False
    extra_instructions: str = ""
    # Per-request additions to individual prompt blocks, e.g. {"style": "Sehr locker."}
    block_overrides: dict[str, str] = Field(default_factory=dict)
    # Extra skills to enable for this request on top of the stage defaults.
    extra_skills: list[str] = Field(default_factory=list)


class EpisodeRequest(BaseModel):
    topic: str
    options: EpisodeOptions = Field(default_factory=EpisodeOptions)


# --- Research ---------------------------------------------------------------


class Source(BaseModel):
    title: str
    authors: str = ""
    year: str = ""
    url: str = ""
    why_relevant: str = ""


class Candidate(BaseModel):
    """A paper or source proposed by a scout."""

    title: str
    authors: str = ""
    year: str = ""
    arxiv_id: str = Field(default="", description="z.B. 1706.03762, leer wenn nicht auf arXiv")
    doi: str = ""
    url: str = Field(default="", description="Landing page oder Abstract-Seite")
    pdf_url: str = Field(default="", description="Direkter Link zum Volltext-PDF, falls bekannt")
    score: float = Field(ge=0, le=10, description="Relevanz für die Episode, 0 bis 10")
    reason: str = Field(description="Ein Satz, warum die Quelle relevant ist")
    quote: str = Field(default="", description="Wörtliches Zitat aus dem Abstract")


class ScoutResult(BaseModel):
    candidates: list[Candidate]


class RankedCandidate(Candidate):
    id: str  # stable key, e.g. "arxiv:1706.03762"
    found_by: list[str] = Field(default_factory=list)  # scout angles


class SelectedPaper(BaseModel):
    id: str = Field(description="ID aus der Kandidatenliste, z.B. arxiv:1706.03762")
    reason: str


class Selection(BaseModel):
    focus: str = Field(description="Ein bis zwei Sätze: Fokus und roter Faden der Episode")
    selected: list[SelectedPaper]


class ResearchResult(BaseModel):
    title_suggestion: str = Field(description="Arbeitstitel für die Episode")
    notes: str = Field(description="Ausführliche Recherche-Notizen als Markdown")
    podcast_material: str = Field(
        description="Markdown: Beispiele, Analogien, überraschende Fakten, Anekdoten und "
        "prägnante Zitate für den Podcast, jeweils mit Beleg"
    )
    sources: list[Source]


# --- Script -----------------------------------------------------------------


class Line(BaseModel):
    speaker: Literal["host", "expert"]
    text: str
    style: str = Field(default="", description="Optionaler Sprechstil, z.B. neugierig")


class Chapter(BaseModel):
    title: str
    lines: list[Line]


class Script(BaseModel):
    title: str
    summary: str = Field(description="Zwei bis drei Sätze Shownotes")
    chapters: list[Chapter]
    handout_items: list[str] = Field(
        default_factory=list,
        description="Formeln, Tabellen oder Abbildungen, auf die im Gespräch verwiesen wird "
        "und die ins Handout gehören (leer, wenn es kein Handout gibt)",
    )

    def iter_lines(self):
        for ci, chapter in enumerate(self.chapters):
            for li, line in enumerate(chapter.lines):
                yield ci, li, line

    @property
    def word_count(self) -> int:
        return sum(len(line.text.split()) for _, _, line in self.iter_lines())


# --- Handout ------------------------------------------------------------------


class Plot(BaseModel):
    name: str = Field(description="Kurzer Bezeichner aus Kleinbuchstaben, Ziffern und Bindestrichen")
    caption: str = Field(description="Bildunterschrift inklusive Quelle bzw. 'schematisch'")
    code: str = Field(description="matplotlib-Code, der genau eine Abbildung zeichnet (ohne savefig/show)")


class Formula(BaseModel):
    name: str = Field(description="Kurzer Bezeichner aus Kleinbuchstaben, Ziffern und Bindestrichen")
    latex: str = Field(description="Formel in LaTeX-Mathe-Syntax ohne $-Zeichen (matplotlib mathtext)")


class Handout(BaseModel):
    markdown: str = Field(
        description="Handout als Markdown; Abbildungen und Formeln als eigene Zeile "
        "{{plot:name}} bzw. {{formula:name}} einbinden"
    )
    plots: list[Plot] = Field(default_factory=list)
    formulas: list[Formula] = Field(default_factory=list)


class PlotFixes(BaseModel):
    plots: list[Plot]


def json_schema(model: type[BaseModel]) -> dict:
    return {"type": "json_schema", "schema": model.model_json_schema()}
