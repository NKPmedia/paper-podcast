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


class Language(str, Enum):
    """Language of everything listeners see and hear (script, titles, handout)."""

    de = "de"
    en = "en"

    @property
    def english_name(self) -> str:
        return {"de": "German", "en": "English"}[self.value]


class ResearchDepth(str, Enum):
    quick = "quick"
    medium = "medium"
    deep = "deep"


class EpisodeOptions(BaseModel):
    length: Length = Length.mittel
    research_depth: ResearchDepth = ResearchDepth.medium
    handout: bool = False
    language: Language = Language.de
    extra_instructions: str = ""
    # Per-request additions to individual prompt blocks, e.g. {"style": "Very casual."}
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
    arxiv_id: str = Field(default="", description="e.g. 1706.03762; empty if not on arXiv")
    doi: str = ""
    url: str = Field(default="", description="Landing page or abstract page")
    pdf_url: str = Field(default="", description="Direct link to the full-text PDF, if known")
    score: float = Field(ge=0, le=10, description="Relevance for the episode, 0 to 10")
    reason: str = Field(description="One sentence on why the source is relevant")
    quote: str = Field(default="", description="Verbatim quote from the abstract")


class ScoutResult(BaseModel):
    candidates: list[Candidate]


class RankedCandidate(Candidate):
    id: str  # stable key, e.g. "arxiv:1706.03762"
    found_by: list[str] = Field(default_factory=list)  # scout angles


class SelectedPaper(BaseModel):
    id: str = Field(description="ID from the candidate list, e.g. arxiv:1706.03762")
    reason: str


class Selection(BaseModel):
    focus: str = Field(description="One or two sentences: the focus and common thread of the episode")
    selected: list[SelectedPaper]


class ResearchResult(BaseModel):
    title_suggestion: str = Field(description="Episode title in the episode language: short (at most 60 "
                                              "characters), concrete and intriguing")
    notes: str = Field(description="Detailed research notes as Markdown, in English")
    podcast_material: str = Field(
        description="Markdown, in English: examples, analogies, surprising facts, anecdotes and "
        "memorable quotes for the podcast, each with its reference"
    )
    sources: list[Source]


# --- Script -----------------------------------------------------------------


class Line(BaseModel):
    speaker: Literal["host", "expert"]
    text: str
    style: str = Field(default="", description="Optional speaking style, e.g. curious, thoughtful, excited")


class Chapter(BaseModel):
    title: str
    lines: list[Line]


class Script(BaseModel):
    title: str = Field(description="Episode title in the episode language: short (at most 60 characters), concrete "
                                   "and intriguing; not a restatement of the topic, no colon subtitle needed")
    summary: str = Field(description="Show notes in the episode language: two or three sentences")
    chapters: list[Chapter]
    handout_items: list[str] = Field(
        default_factory=list,
        description="Formulas, tables or figures the conversation refers to and that belong "
        "in the handout (empty when there is no handout)",
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
    name: str = Field(description="Short identifier of lowercase letters, digits and hyphens")
    caption: str = Field(description="Caption as LaTeX text, including the source or a note that the figure is schematic")
    code: str = Field(description="matplotlib code that draws exactly one figure (no savefig/show)")


class Handout(BaseModel):
    latex_body: str = Field(
        description="The handout content as LaTeX (document body only, no preamble); "
        "figures on their own line as \\plot{name}"
    )
    plots: list[Plot] = Field(default_factory=list)


class HandoutFix(BaseModel):
    latex_body: str = Field(description="The complete, corrected LaTeX document body")


class PlotFixes(BaseModel):
    plots: list[Plot]


def json_schema(model: type[BaseModel]) -> dict:
    return {"type": "json_schema", "schema": model.model_json_schema()}
