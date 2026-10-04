"""Thin wrapper around the Claude Agent SDK.

Every call runs Claude Code headless in a job directory with an explicit tool
allowlist and structured (JSON schema) output. The ``ClaudeRunner`` protocol lets
tests substitute a fake.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.config import SECRET_ENV_VARS
from app.errors import PodcastError

log = logging.getLogger(__name__)


class ClaudeError(PodcastError):
    def __init__(self, message: str, details: str = "", transient: bool = False):
        super().__init__(message, details)
        self.cause = message  # the reason without the name of the call (to group equal failures)
        self.transient = transient  # worth retrying after a pause

    def for_call(self, label: str, attempts: int = 1) -> "ClaudeError":
        """Name the failed call and say that it was already retried."""
        message = f"{label}: {self.cause}" if label else self.cause
        if attempts > 1:
            message += f" (Automatisch {attempts}-mal versucht.)"
        self.message = message
        self.args = (message,)
        return self


# (regex over the raw error and Claude's stderr, summary shown to the user, transient?)
CAUSES = (
    (r"invalid api key|invalid bearer|authentication|unauthorized|\b401\b|oauth token|not logged in|/login"
     r"|token (has )?expired",
     "Claude konnte sich nicht anmelden: Der Claude-Token ist ungültig oder abgelaufen. Erzeuge auf deinem Rechner "
     "mit `claude setup-token` einen neuen und trag ihn unter Einstellungen → Claude ein.", False),
    (r"usage limit|quota",
     "Das Claude-Nutzungslimit deines Abos ist erreicht.{reset} Danach mit „Fortsetzen“ weitermachen; fertige "
     "Schritte bleiben erhalten.", False),
    (r"rate.?limit|\b429\b|too many requests",
     "Claude hat zu viele Anfragen kurz hintereinander abgelehnt (Ratenlimit). In ein paar Minuten mit „Fortsetzen“ "
     "weitermachen. Passiert das öfter, unter Einstellungen → Claude weniger parallele Aufrufe einstellen.", True),
    (r"overloaded|\b529\b|internal server error|\b50[023]\b",
     "Die Claude-Server sind gerade überlastet oder gestört (das liegt bei Anthropic, nicht an deinem Server). "
     "In einigen Minuten mit „Fortsetzen“ erneut versuchen; status.anthropic.com zeigt Störungen.", True),
    (r"enotfound|econnrefused|econnreset|etimedout|getaddrinfo|network error|connection error|unable to connect"
     r"|fetch failed",
     "Dein Server erreicht Claude nicht (Netzwerkproblem: Verbindung abgelehnt, abgebrochen oder DNS-Fehler). Prüfe "
     "die Internetverbindung und DNS des Servers bzw. des Docker-Netzwerks, dann „Fortsetzen“.", True),
    (r"exit code:? ?(-9|137)\b|sigkill|out of memory|enomem|heap out of memory|\bkilled\b",
     "Claude Code wurde vom System beendet, vermutlich weil der Arbeitsspeicher nicht reichte. Unter Einstellungen → "
     "Claude weniger parallele Aufrufe einstellen (bei 4 GB RAM höchstens 3) oder dem Server mehr RAM geben, dann "
     "„Fortsetzen“.", True),
    (r"model not found|invalid model|not_found_error",
     "Das eingestellte Claude-Modell gibt es nicht oder dein Abo hat keinen Zugriff darauf. Prüfe die Modellnamen "
     "unter Einstellungen → Claude (z.B. haiku, sonnet, opus).", False),
    (r"error_max_turns|max_turns|maximum number of turns",
     "Claude hat das Rundenlimit erreicht, bevor die Antwort fertig war (zu viele Such- oder Leseschritte). Mit "
     "„Fortsetzen“ erneut versuchen; passiert das wieder, unter Einstellungen → Claude mehr Runden erlauben.", False),
    (r"clinotfound|claude code not found",
     "Claude Code wurde im Container nicht gefunden. Das Image ist vermutlich beschädigt – bitte neu ziehen "
     "(`docker compose pull && docker compose up -d`).", False),
)

# What an unrecognized failure looks like, in words (first match wins).
RAW_REASONS = (
    (r"subtype=error_during_execution", "Claude Code ist während der Arbeit abgestürzt"),
    (r"subtype=error_max_budget", "Claude Code hat sein Kostenlimit erreicht"),
    (r"exit code:? ?(-?\d+)", "Claude Code wurde unerwartet beendet (Exit-Code {0})"),
    (r"without a result message", "Claude Code hat sich ohne Ergebnis beendet"),
    (r"timeout|timed out", "Claude Code hat zu lange nicht geantwortet"),
)


def _reset_time(text: str) -> str:
    """' Wieder verfügbar ab 15:00 Uhr.' from 'usage limit reached|<epoch>', else ''."""
    match = re.search(r"limit reached\|(\d{10})", text)
    if not match:
        return ""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        when = datetime.fromtimestamp(int(match.group(1)), ZoneInfo(os.environ.get("TZ") or "Europe/Berlin"))
    except (ValueError, KeyError, OSError):
        return ""
    return f" Wieder verfügbar ab {when:%H:%M} Uhr ({when:%d.%m.})."


def _unknown(raw: str, haystack: str) -> str:
    for pattern, text in RAW_REASONS:
        match = re.search(pattern, haystack)
        if match:
            reason = text.format(*match.groups())
            break
    else:
        first = next((line.strip() for line in raw.splitlines() if line.strip()), "unbekannter Fehler")
        reason = f"Claude Code meldet „{first[:200]}“"
    return (f"{reason}. Das ist meist ein einmaliger Aussetzer: mit „Fortsetzen“ erneut versuchen. Tritt es "
            "wiederholt auf, stehen die Einzelheiten unter „Technische Details“.")


def claude_error(raw: str, stderr: list[str] | None = None) -> ClaudeError:
    """Turn a raw Claude failure into a ClaudeError with a clear summary."""
    tail = "\n".join(stderr or [])[-3000:]
    haystack = f"{raw}\n{tail}".lower()
    found = next(((text, retry) for pattern, text, retry in CAUSES if re.search(pattern, haystack)), None)
    if found:
        summary, transient = found[0].replace("{reset}", _reset_time(haystack)), found[1]
    else:  # unknown crashes are usually one-offs: worth one more try
        summary, transient = _unknown(raw, haystack), True
    details = raw + (f"\n\nClaude Code output (last lines):\n{tail}" if tail.strip() else "")
    return ClaudeError(summary, details, transient)


def explain(error: str) -> str:
    """Short summary for a raw Claude error (used for checks and messages)."""
    return claude_error(error).message


def claude_auth_configured() -> bool:
    """True if Claude Code has credentials: a token in the environment or a login in its config dir."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"):
        return True
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return (config_dir / ".credentials.json").exists()


@dataclass
class ClaudeCall:
    prompt: str
    cwd: Path
    tools: list[str]
    skills: list[str] = field(default_factory=list)
    system_append: str = ""
    output_schema: dict | None = None
    max_turns: int | None = None
    model: str | None = None
    resume: str | None = None  # session id to continue
    label: str = ""  # names the call in error messages, e.g. "Scout „Kritik“" or "Skript"
    retry: bool = True  # retry transient failures after a pause (off for quick checks)


@dataclass
class ClaudeResult:
    structured: Any
    text: str
    session_id: str
    cost_usd: float | None
    num_turns: int
    skills_used: list[str]
    # Token usage summed over all turns and models: input, output, cache_read, cache_write.
    tokens: dict[str, int] = field(default_factory=dict)


class ClaudeRunner(Protocol):
    async def run(self, call: ClaudeCall) -> ClaudeResult: ...


def scrubbed_env() -> dict[str, str]:
    """Blank out app secrets so the Claude subprocess never sees them."""
    return {name: "" for name in SECRET_ENV_VARS if name in os.environ}


class AgentSDKRunner:
    # Pauses before retrying a transient failure (rate limit, overload, network, crash).
    RETRY_DELAYS = (30, 90)

    async def run(self, call: ClaudeCall) -> ClaudeResult:
        notes = []
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            try:
                return await self._run_once(call)
            except ClaudeError as exc:
                if not (exc.transient and call.retry) or attempt == len(self.RETRY_DELAYS):
                    if notes:
                        exc.details = "\n\n".join([*notes, exc.details])
                    raise exc.for_call(call.label, attempt + 1) from exc.__cause__
                delay = self.RETRY_DELAYS[attempt]
                notes.append(f"Attempt {attempt + 1} failed ({exc.message}), retried after {delay}s:\n{exc.details}")
                log.warning("Claude call failed (%s), retrying in %ss", exc.message, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _run_once(self, call: ClaudeCall) -> ClaudeResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            ToolUseBlock,
            query,
        )

        stderr_lines: deque[str] = deque(maxlen=40)

        def on_stderr(line: str) -> None:
            log.debug("claude: %s", line)
            stderr_lines.append(line.rstrip())

        tools = list(call.tools)
        if call.skills and "Skill" not in tools:
            tools.append("Skill")
        options = ClaudeAgentOptions(
            cwd=str(call.cwd),
            tools=tools,
            allowed_tools=[t for t in tools if t != "Skill"],
            skills=call.skills or [],
            setting_sources=["project"],
            permission_mode="dontAsk",
            system_prompt={"type": "preset", "preset": "claude_code", "append": call.system_append},
            output_format=call.output_schema,
            max_turns=call.max_turns,
            model=call.model,
            resume=call.resume,
            env=scrubbed_env(),
            stderr=on_stderr,
        )

        result: ResultMessage | None = None
        skills_used: list[str] = []
        try:
            async for message in query(prompt=call.prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            log.info("claude tool: %s %s", block.name, _short(block.input))
                            skill = str(block.input.get("skill", "")) if block.name == "Skill" else ""
                            if skill and skill not in skills_used:
                                skills_used.append(skill)
                elif isinstance(message, ResultMessage):
                    result = message
        except ClaudeError:
            raise
        except Exception as exc:  # CLI crashed, could not start, auth failed before the first turn …
            raise claude_error(f"{type(exc).__name__}: {exc}", list(stderr_lines)) from exc

        if result is None:
            raise claude_error("Claude Code ended without a result message", list(stderr_lines))
        if result.is_error:
            raise claude_error(
                f"Claude failed: subtype={result.subtype}, stop={result.terminal_reason}, "
                f"api_status={result.api_error_status}, errors={result.errors or result.result}",
                list(stderr_lines),
            )
        if call.output_schema and result.structured_output is None:
            raise ClaudeError(
                "Claude hat geantwortet, aber nicht im verlangten JSON-Format, sodass der Server die Antwort nicht "
                "auswerten kann. Mit „Fortsetzen“ erneut versuchen.",
                f"result text: {(result.result or '')[:2000]}", transient=True)
        return ClaudeResult(
            structured=result.structured_output,
            text=result.result or "",
            session_id=result.session_id,
            cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
            skills_used=skills_used,
            tokens=token_usage(result.model_usage, result.usage),
        )


TOKEN_KEYS = {
    "input": ("inputTokens", "input_tokens"),
    "output": ("outputTokens", "output_tokens"),
    "cache_read": ("cacheReadInputTokens", "cache_read_input_tokens"),
    "cache_write": ("cacheCreationInputTokens", "cache_creation_input_tokens"),
}


def token_usage(model_usage: dict | None, usage: dict | None) -> dict[str, int]:
    """Tokens of one Claude Code run. ``model_usage`` (per model, camelCase) also counts
    helper models, so it wins over the plain ``usage`` dict when present."""
    if model_usage:
        rows = list(model_usage.values())
        index = 0
    elif usage:
        rows = [usage]
        index = 1
    else:
        return {}
    totals = {key: 0 for key in TOKEN_KEYS}
    for row in rows:
        for key, names in TOKEN_KEYS.items():
            value = row.get(names[index]) if isinstance(row, dict) else None
            if isinstance(value, (int, float)):
                totals[key] += int(value)
    return totals


def _short(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"
