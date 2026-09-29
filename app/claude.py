"""Thin wrapper around the Claude Agent SDK.

Every call runs Claude Code headless in a job directory with an explicit tool
allowlist and structured (JSON schema) output. The ``ClaudeRunner`` protocol lets
tests substitute a fake.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.config import SECRET_ENV_VARS

log = logging.getLogger(__name__)


class ClaudeError(RuntimeError):
    pass


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
            stderr=lambda line: log.debug("claude: %s", line),
        )

        result: ResultMessage | None = None
        skills_used: list[str] = []
        async for message in query(prompt=call.prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        log.info("claude tool: %s %s", block.name, _short(block.input))
                        if block.name == "Skill":
                            skills_used.append(str(block.input.get("skill", "")))
            elif isinstance(message, ResultMessage):
                result = message

        if result is None:
            raise ClaudeError("Claude returned no result")
        if result.is_error:
            raise ClaudeError(
                f"Claude failed ({result.subtype}, stop={result.terminal_reason}): "
                f"{result.errors or result.result}"
            )
        if call.output_schema and result.structured_output is None:
            raise ClaudeError("Claude returned no structured output")
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
