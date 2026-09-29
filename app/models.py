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


class ResearchResult(BaseModel):
    title_suggestion: str = Field(description="Arbeitstitel für die Episode")
    notes: str = Field(description="Ausführliche Recherche-Notizen als Markdown")
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

    def iter_lines(self):
        for ci, chapter in enumerate(self.chapters):
            for li, line in enumerate(chapter.lines):
                yield ci, li, line

    @property
    def word_count(self) -> int:
        return sum(len(line.text.split()) for _, _, line in self.iter_lines())


def json_schema(model: type[BaseModel]) -> dict:
    return {"type": "json_schema", "schema": model.model_json_schema()}
