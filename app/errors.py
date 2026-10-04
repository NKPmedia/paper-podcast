"""User-facing errors: a clear German message plus optional technical details.

Jobs store the error as one text: the message, then ``\\n\\nDetails:\\n`` and the
details. The web UI and Telegram show the message prominently and the details on
request.
"""

from __future__ import annotations

import traceback

DETAILS_MARKER = "\n\nDetails:\n"
STEP_NAMES = {"research": "Recherche", "script": "Skript", "handout": "Handout", "tts": "Sprachausgabe",
              "audio": "Audio", "memory": "Verknüpfung", "series_plan": "Reihenplanung"}


class PodcastError(RuntimeError):
    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:
        return self.message


def log_text(exc: BaseException, limit: int = 4000) -> str:
    """Message plus technical details for the episode log (the job error is replaced on resume)."""
    text = f"{type(exc).__name__}: {exc}"
    details = getattr(exc, "details", "")
    if details:
        text += f"\n\n{details}"
    return text if len(text) <= limit else text[:limit] + "…"


def format_error(exc: BaseException, stage_label: str = "") -> str:
    """The text stored for a failed job: a short summary, then the raw error and the stack trace."""
    if isinstance(exc, PodcastError):
        message, details = exc.message, exc.details
    else:  # a bug or something we did not anticipate
        where = f" im Schritt „{STEP_NAMES.get(stage_label, stage_label)}“" if stage_label else ""
        message = (f"Unerwarteter Fehler{where} ({type(exc).__name__}: {str(exc)[:200]}). Das ist vermutlich ein "
                   "Programmfehler und kein Problem deiner Einstellungen. „Fortsetzen“ kann trotzdem helfen; sonst "
                   "die technischen Details unten als Fehlerbericht weitergeben.")
        details = ""
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
    parts = [p.strip() for p in (details, trace) if p and p.strip()]
    details = "\n\n".join(parts)
    if len(details) > 10000:
        details = "…\n" + details[-10000:]
    return message + (DETAILS_MARKER + details if details else "")


def split_error(text: str) -> tuple[str, str]:
    message, _, details = (text or "").partition(DETAILS_MARKER)
    return message.strip(), details.strip()


class NeedsInput(Exception):
    """The job pauses until the listener answers clarifying questions (see app.pipeline.clarify)."""

    def __init__(self, questions: list[dict]):
        super().__init__(f"{len(questions)} clarifying question(s) need an answer")
        self.questions = questions
