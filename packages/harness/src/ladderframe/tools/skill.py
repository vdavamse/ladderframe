from __future__ import annotations

from dataclasses import replace

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from ..core.deps import HarnessDeps
from .base import tool

_DESCRIPTION = """Invoke a skill: a packaged set of instructions for a particular kind of task.
When a task matches one of the skills below, call this tool first and follow the instructions it returns.

Available skills:
"""


async def _describe(ctx: RunContext[HarnessDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    skills = [s for s in ctx.deps.runtime.skills.values() if not s.disable_model_invocation]
    if not skills:
        return None
    listing = "\n".join(f"- {s.name}: {s.listing}" for s in sorted(skills, key=lambda s: s.name))
    return replace(tool_def, description=_DESCRIPTION + listing)


@tool(subject="skill", prepare=_describe)
async def Skill(ctx: RunContext[HarnessDeps], skill: str, args: str | None = None) -> str:
    """Invoke a skill by name.

    Args:
        skill: The skill name, e.g. `commit-message`.
        args: Optional arguments for the skill, as the user would type them after `/skill-name`.
    """
    runtime = ctx.deps.runtime
    spec = runtime.skills.get(skill.lstrip("/"))
    if spec is None:
        raise ModelRetry(f"Unknown skill {skill!r}. Available: {', '.join(sorted(runtime.skills)) or 'none'}")
    if spec.disable_model_invocation:
        return f"Skill {spec.name!r} can only be invoked by the user."

    # `allowed-tools` / `disallowed-tools` are applied by PermissionGuard once this call returns: it runs
    # on the workflow side under Temporal, where mutating deps here (inside an activity) would be lost.
    rendered = await runtime.render_skill(spec, args or "", ctx.deps)
    if spec.context == "fork":
        return await runtime.run_forked_skill(spec, rendered, ctx.deps)
    return f'<skill name="{spec.name}" directory="{spec.directory}">\n{rendered}\n</skill>'
