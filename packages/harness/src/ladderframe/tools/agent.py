"""The `Agent` tool (alias `Task`, its pre-v2.1.63 Claude Code name): run a sub-agent from `agents/*.md`."""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from ..core.deps import HarnessDeps
from .base import tool

_DESCRIPTION = """Launch a sub-agent to handle a task autonomously in its own context window.
The sub-agent does not see this conversation, so the prompt must be self-contained.
It returns a single final report; relay what matters to the user.

Available agent types:
"""


def _available(deps: HarnessDeps) -> list[str]:
    names = sorted(deps.runtime.subagents)
    if deps.allowed_subagents is not None:
        names = [n for n in names if n in deps.allowed_subagents]
    return names


async def _describe(ctx: RunContext[HarnessDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    deps = ctx.deps
    if deps.depth >= deps.config.limits.max_subagent_depth:
        return None
    names = _available(deps)
    if not names:
        return None
    listing = "\n".join(f"- {n}: {deps.runtime.subagents[n].description}" for n in names)
    return replace(tool_def, description=_DESCRIPTION + listing)


@tool(subject="subagent_type", prepare=_describe, aliases=("Task",))
async def Agent(ctx: RunContext[HarnessDeps], description: str, prompt: str, subagent_type: str) -> str:
    """Run a sub-agent.

    Args:
        description: A short (3-5 word) description of the task.
        prompt: The complete task for the sub-agent.
        subagent_type: Which agent type to use.
    """
    deps = ctx.deps
    names = _available(deps)
    if subagent_type not in names:
        raise ModelRetry(f"Unknown or disallowed agent type {subagent_type!r}. Available: {', '.join(names) or 'none'}")
    spec = deps.runtime.subagents[subagent_type]
    return await deps.runtime.run_subagent(spec, prompt, deps)
