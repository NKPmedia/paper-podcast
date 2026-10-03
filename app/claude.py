"""Thin wrapper around the Claude Agent SDK.

Every call runs Claude Code headless in a job directory with an explicit tool
allowlist and structured (JSON schema) output. The ``ClaudeRunner`` protocol lets
tests substitute a fake.
"""

from __future__ import annotations

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
    pass


# (regex over the raw error and Claude's stderr, summary shown to the user)
CAUSES = (
    (r"invalid api key|invalid bearer|authentication|unauthorized|\b401\b|oauth token|not logged in|/login"
     r"|token (has )?expired",
     "Claude konnte sich nicht anmelden: Der Claude-Token ist ungültig oder abgelaufen. Erzeuge einen neuen mit "
     "`claude setup-token` und trag ihn unter Einstellungen → Claude ein."),
    (r"usage limit|rate.?limit|\b429\b|quota",
     "Das Claude-Nutzungslimit ist erreicht. Später mit „Fortsetzen“ weitermachen; fertige Schritte bleiben erhalten."),
    (r"overloaded|\b529\b|internal server error|\b50[023]\b",
     "Die Claude-Server sind gerade überlastet oder gestört. Bitte später mit „Fortsetzen“ erneut versuchen."),
    (r"enotfound|econnrefused|econnreset|etimedout|getaddrinfo|network error|connection error|unable to connect"
     r"|fetch failed",
     "Claude ist vom Server aus nicht erreichbar (Netzwerkproblem). Prüfe die Internetverbindung des Servers."),
    (r"model not found|invalid model|not_found_error",
     "Das eingestellte Claude-Modell ist nicht verfügbar. Prüfe die Modellnamen unter Einstellungen → Claude."),
    (r"error_max_turns|max_turns|maximum number of turns",
     "Claude hat das Rundenlimit erreicht, bevor die Antwort fertig war. Mit „Fortsetzen“ erneut versuchen oder "
     "unter Einstellungen → Claude mehr Runden erlauben."),
    (r"clinotfound|claude code not found",
     "Claude Code wurde im Container nicht gefunden. Das Image ist vermutlich beschädigt – bitte neu ziehen."),
)


def claude_error(raw: str, stderr: list[str] | None = None) -> ClaudeError:
    """Turn a raw Claude failure into a ClaudeError with a clear summary."""
    tail = "\n".join(stderr or [])[-3000:]
    haystack = f"{raw}\n{tail}".lower()
    summary = next((text for pattern, text in CAUSES if re.search(pattern, haystack)),
                   "Claude ist mit einem Fehler abgebrochen.")
    details = raw + (f"\n\nClaude Code output (last lines):\n{tail}" if tail.strip() else "")
    return ClaudeError(summary, details)


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


@dataclass
class ClaudeResult:
    structured: Any
    text: str
    session_id: str
    cost_usd: float | None
    num_turns: int
    skills_used: list[str]


class ClaudeRunner(Protocol):
    async def run(self, call: ClaudeCall) -> ClaudeResult: ...


def scrubbed_env() -> dict[str, str]:
    """Blank out app secrets so the Claude subprocess never sees them."""
    return {name: "" for name in SECRET_ENV_VARS if name in os.environ}


class AgentSDKRunner:
    async def run(self, call: ClaudeCall) -> ClaudeResult:
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
                            if block.name == "Skill":
                                skills_used.append(str(block.input.get("skill", "")))
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
            raise ClaudeError("Claude hat keine strukturierte Antwort geliefert (das JSON-Format fehlte).",
                              f"result text: {(result.result or '')[:2000]}")
        return ClaudeResult(
            structured=result.structured_output,
            text=result.result or "",
            session_id=result.session_id,
            cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
            skills_used=skills_used,
        )


def _short(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"
