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


class Audience(str, Enum):
    """What the listener already knows from earlier episodes."""

    regular = "regular"  # has heard the earlier episodes: refresh known basics briefly, go deeper
    newcomer = "newcomer"  # explain everything, earlier episodes are only pointers


class ResearchDepth(str, Enum):
    quick = "quick"
    medium = "medium"
    deep = "deep"


class EpisodeOptions(BaseModel):
    length: Length = Length.mittel
    research_depth: ResearchDepth = ResearchDepth.medium
    handout: bool = False
    language: Language = Language.de
    # Let Claude ask clarifying questions before the research starts (only when the topic is unclear).
    clarify: bool = True
    extra_instructions: str = ""
    # Per-request additions to individual prompt blocks, e.g. {"style": "Very casual."}
    block_overrides: dict[str, str] = Field(default_factory=dict)
    # Extra skills to enable for this request on top of the stage defaults.
    extra_skills: list[str] = Field(default_factory=list)
    # Job ID of the earlier episode this one continues (a follow-up suggestion), or "".
    follows: str = ""
    audience: Audience = Audience.regular
    # Speech engine for this episode: "edge" or "gemini"; "" = the default from the settings.
    # The other engine (if available) takes over when the chosen one fails.
    tts: Literal["", "edge", "gemini"] = ""


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


MAX_QUESTIONS = 10


class ClarifyingQuestion(BaseModel):
    question: str = Field(description="The question, short and concrete, in the episode language")
    options: list[str] = Field(description="2 to 5 premade answers, the most likely one first")
    allow_free_text: bool = Field(default=True, description="Whether the listener may also answer in their own words")


class ClarificationRequest(BaseModel):
    needs_clarification: bool = Field(description="False when the topic is clear enough to research right away")
    reason: str = Field(description="One sentence (English) on why questions are or are not needed")
    questions: list[ClarifyingQuestion] = Field(default_factory=list, description="At most 10 questions; empty when "
                                                "needs_clarification is false")


class ScoutTask(BaseModel):
    title: str = Field(description="2 to 4 words, e.g. 'Core method' or 'Critique and replications'")
    objective: str = Field(description="What this scout must find, and why it matters for the episode")
    search_hints: str = Field(description="Concrete search terms, author names, venues or APIs to try first")
    avoid: str = Field(default="", description="What is out of scope for this scout (covered by others)")


class ResearchPlan(BaseModel):
    focus: str = Field(description="One or two sentences: the angle and common thread of the episode")
    key_questions: list[str] = Field(description="3 to 8 questions the episode must answer")
    tasks: list[ScoutTask] = Field(description="One task per scout, each a distinct perspective")


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


class EpisodeReference(BaseModel):
    id: str = Field(description="ID of the earlier episode, exactly as in the list of earlier episodes")
    how: str = Field(description="One sentence in the episode language: how this episode refers to it")


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
    episode_references: list[EpisodeReference] = Field(
        default_factory=list,
        description="Every earlier episode the dialogue refers to (empty when it refers to none)",
    )

    def iter_lines(self):
        for ci, chapter in enumerate(self.chapters):
            for li, line in enumerate(chapter.lines):
                yield ci, li, line

    @property
    def word_count(self) -> int:
        return sum(len(line.text.split()) for _, _, line in self.iter_lines())


# --- Episode memory (links between episodes) -----------------------------------

MAX_FOLLOW_UPS = 5


class FollowUp(BaseModel):
    title: str = Field(description="Working title in the episode language, at most 60 characters")
    topic: str = Field(description="The request for the new episode in the episode language, one to three "
                                   "sentences, understandable on its own (it becomes the topic of a new episode)")
    why: str = Field(description="One sentence in the episode language: what the listener gains, and how it "
                                 "builds on this episode")


class EpisodeMemory(BaseModel):
    mini_summary: str = Field(description="English, one sentence of at most 250 characters: what the episode "
                                          "covered, specific enough to tell whether a later topic relates to it")
    reference_summary: str = Field(
        description="English Markdown, 200 to 400 words, written for a later script writer who wants to refer "
        "back to this episode: core claims and numbers, which concepts were explained and how (the analogies and "
        "examples used), terms defined, and the questions left open"
    )
    key_concepts: list[str] = Field(default_factory=list, description="3 to 10 central concepts or methods, "
                                                                       "short noun phrases in English")
    follow_ups: list[FollowUp] = Field(default_factory=list, description=f"Up to {MAX_FOLLOW_UPS} suggested "
                                       "follow-up episodes, the most rewarding first")


# --- Series planning ------------------------------------------------------------

MIN_PLANNED, MAX_PLANNED = 2, 12


class PlannedEpisode(BaseModel):
    title: str = Field(description="Working title in the episode language, at most 60 characters")
    topic: str = Field(description="The request for this episode in the episode language, two to four sentences, "
                                   "understandable on its own; name the core papers, methods or authors")
    goal: str = Field(description="One or two sentences in the episode language: what the listener understands "
                                  "after this part that they did not before")
    covers: list[str] = Field(default_factory=list, description="3 to 6 key points this part covers, short, in the "
                                                                "episode language")
    builds_on: list[int] = Field(default_factory=list, description="Part numbers (1-based, counting the whole series "
                                                                   "including existing parts) this part builds on")
    sources: list[str] = Field(default_factory=list, description="Titles of the most important sources for this part, "
                                                                 "from the research")


class SeriesPlanResult(BaseModel):
    title: str = Field(description="Title of the series in the episode language, at most 60 characters")
    arc: str = Field(description="The common thread in the episode language, two to four sentences: where the series "
                                 "starts, where it leads and why in this order")
    episodes: list[PlannedEpisode] = Field(description="The planned parts that do not exist yet, in order")
    rationale: str = Field(description="In the episode language, three to six sentences: why these parts and this "
                                       "order, what was left out and why")
    changes: str = Field(default="", description="Only when revising: in the episode language, what changed compared "
                                                 "with the previous plan and why; otherwise empty")


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
