"""Applies permission rules to any toolset (hides fully blocked tools and checks each call) and truncates text
results of tools that do not bound their own output (MCP tools, custom tools, WebFetch, ...)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from ..core.deps import HarnessDeps
from ..core.permissions import PermissionDenied
from . import truncate
from .base import get_meta, subject_of


@dataclass
class PermissionGuard(WrapperToolset[HarnessDeps]):
    functions: dict[str, Callable[..., Any]] = field(default_factory=dict)
    """Tool name -> decorated function, to find each tool's permission subject."""
    delegate: bool = False
    """The agent has the Agent tool, so truncation hints may suggest handing the output to a sub-agent."""

    async def get_tools(self, ctx: RunContext[HarnessDeps]) -> dict[str, ToolsetTool[HarnessDeps]]:
        tools = await super().get_tools(ctx)
        return {name: t for name, t in tools.items() if not ctx.deps.permissions.tool_blocked(name)}

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[HarnessDeps], tool: ToolsetTool[HarnessDeps]
    ) -> Any:
        meta = get_meta(self.functions.get(name))
        subject = subject_of(meta, tool_args, ctx.deps)
        try:
            ctx.deps.permissions.check(name, subject)
        except PermissionDenied as exc:
            return str(exc)
        result = await super().call_tool(name, tool_args, ctx, tool)
        if name == "Skill":
            _apply_skill_rules(ctx.deps, str(tool_args.get("name", "")))
        if isinstance(result, str) and not (meta and meta.truncates):
            result = await _truncate(result, ctx.deps, truncate.can_delegate(ctx.deps, self.delegate))
        return result


async def _truncate(text: str, deps: HarnessDeps, delegate: bool) -> str:
    max_lines, max_bytes = truncate.limits(deps)
    cut = truncate.cut(text, max_lines, max_bytes)
    if cut is None:
        return text
    preview, removed, unit = cut
    if _in_workflow():
        # Workflow code must not touch the filesystem: the worker writes the file in an activity.
        from datetime import timedelta

        from temporalio import workflow

        from ..runtime.temporal.activities import SaveToolOutputInput, save_tool_output

        path = await workflow.execute_activity(
            save_tool_output,
            SaveToolOutputInput(deps.root_path, text),
            start_to_close_timeout=timedelta(seconds=60),
        )
    else:
        path = truncate.write_output(truncate.output_dir(deps), text)
    return truncate.render(preview, removed, unit, path, delegate)


def _in_workflow() -> bool:
    try:
        from temporalio import workflow
    except ImportError:
        return False
    return workflow.in_workflow()


def _apply_skill_rules(deps: HarnessDeps, skill_name: str) -> None:
    """A successfully invoked skill's `allowed-tools` / `disallowed-tools` hold for the rest of the run."""
    spec = deps.runtime.skills.get(skill_name.lstrip("/"))
    if spec is None or spec.disable_model_invocation:
        return
    deps.grants.extend(r for r in spec.allowed_tools if r not in deps.grants)
    deps.restrictions.extend(r for r in spec.disallowed_tools if r not in deps.restrictions)
