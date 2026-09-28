"""Applies permission rules to any toolset: hides fully blocked tools and checks each call."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from ..core.deps import HarnessDeps
from ..core.permissions import PermissionDenied
from .base import get_meta, subject_of


@dataclass
class PermissionGuard(WrapperToolset[HarnessDeps]):
    functions: dict[str, Callable[..., Any]] = field(default_factory=dict)
    """Tool name -> decorated function, to find each tool's permission subject."""

    async def get_tools(self, ctx: RunContext[HarnessDeps]) -> dict[str, ToolsetTool[HarnessDeps]]:
        tools = await super().get_tools(ctx)
        return {name: t for name, t in tools.items() if not ctx.deps.permissions.tool_blocked(name)}

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[HarnessDeps], tool: ToolsetTool[HarnessDeps]
    ) -> Any:
        subject = subject_of(get_meta(self.functions.get(name)), tool_args)
        try:
            ctx.deps.permissions.check(name, subject)
        except PermissionDenied as exc:
            return str(exc)
        result = await super().call_tool(name, tool_args, ctx, tool)
        if name == "Skill":
            _apply_skill_rules(ctx.deps, str(tool_args.get("skill", "")))
        return result


def _apply_skill_rules(deps: HarnessDeps, skill_name: str) -> None:
    """A successfully invoked skill's `allowed-tools` / `disallowed-tools` hold for the rest of the run."""
    spec = deps.runtime.skills.get(skill_name.lstrip("/"))
    if spec is None or spec.disable_model_invocation:
        return
    deps.grants.extend(r for r in spec.allowed_tools if r not in deps.grants)
    deps.restrictions.extend(r for r in spec.disallowed_tools if r not in deps.restrictions)
