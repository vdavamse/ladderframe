"""The run context carried into Temporal activities."""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.durable_exec.temporal import TemporalRunContext

from ...core.deps import HarnessDeps
from ...tools.read import already_loaded


class HarnessRunContext(TemporalRunContext[HarnessDeps]):
    """Adds what built-in tools need from the conversation, which doesn't cross into activities:
    for Read, the AGENTS.md / CLAUDE.md files it has already included (`loaded_instructions`)."""

    @classmethod
    def serialize_run_context(cls, ctx: RunContext[Any]) -> dict[str, Any]:
        data = super().serialize_run_context(ctx)
        if ctx.tool_name == "Read":
            data["loaded_instructions"] = sorted(already_loaded(ctx.messages))
        return data
