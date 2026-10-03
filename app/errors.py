"""User-facing errors: a clear German message plus optional technical details.

Jobs store the error as one text: the message, then ``\\n\\nDetails:\\n`` and the
details. The web UI and Telegram show the message prominently and the details on
request.
"""

from __future__ import annotations

import traceback

DETAILS_MARKER = "\n\nDetails:\n"


class PodcastError(RuntimeError):
    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:
        return self.message


def format_error(exc: BaseException, stage_label: str = "") -> str:
    """The text stored for a failed job: a short summary, then the raw error and the stack trace."""
    if isinstance(exc, PodcastError):
        message, details = exc.message, exc.details
    else:  # a bug or something we did not anticipate
        where = f" in step '{stage_label}'" if stage_label else ""
        message = f"Unexpected error{where}: {type(exc).__name__}: {exc}"
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
