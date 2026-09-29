"""`Skill`: load a skill's instructions into the conversation. Output follows opencode's skill tool; the list of
available skills is in the system prompt (`<available_skills>`, see `Runtime.skills_prompt`)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from ..core.deps import HarnessDeps
from .base import tool
from .ripgrep import relative, run_rg

DESCRIPTION = """Load a specialized skill when the task at hand matches one of the skills listed in the system prompt.

Use this tool to inject the skill's instructions and resources into current conversation. The output may contain detailed workflow guidance as well as references to scripts, files, etc in the same directory as the skill.

The skill name must match one of the skills listed in your system prompt."""

SAMPLED_FILES = 10
FORKED_SKILL_TIMEOUT = timedelta(minutes=30)
"""Under Temporal, a `context: fork` skill runs its whole sub-agent loop inside this tool's activity."""


async def _hide_without_skills(ctx: RunContext[HarnessDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if any(not s.disable_model_invocation for s in ctx.deps.runtime.skills.values()) else None


@tool(
    subject="name",
    prepare=_hide_without_skills,
    description=DESCRIPTION,
    metadata={"temporal": {"start_to_close_timeout": FORKED_SKILL_TIMEOUT}},
)
async def Skill(ctx: RunContext[HarnessDeps], name: str) -> str:
    """Load a skill.

    Args:
        name: The name of the skill from available_skills
    """
    runtime = ctx.deps.runtime
    spec = runtime.skills.get(name.lstrip("/"))
    if spec is None:
        available = ", ".join(sorted(runtime.skills)) or "none"
        raise ModelRetry(f'Skill "{name}" not found. Available skills: {available}')
    if spec.disable_model_invocation:
        raise ModelRetry(f'Skill "{spec.name}" can only be invoked by the user.')

    # `allowed-tools` / `disallowed-tools` are applied by PermissionGuard once this call returns: it runs
    # on the workflow side under Temporal, where mutating deps here (inside an activity) would be lost.
    rendered = await runtime.render_skill(spec, "", ctx.deps)
    if spec.context == "fork":
        return await runtime.run_forked_skill(spec, rendered, ctx.deps)

    directory = spec.directory or Path(".")
    files = await _sample_files(directory)
    return "\n".join(
        [
            f'<skill_content name="{spec.name}">',
            f"# Skill: {spec.name}",
            "",
            rendered.strip(),
            "",
            f"Base directory for this skill: {directory}",
            "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.",
            "Note: file list is sampled.",
            "",
            "<skill_files>",
            "\n".join(f"<file>{(directory / f).resolve()}</file>" for f in files),
            "</skill_files>",
            "</skill_content>",
        ]
    )


async def _sample_files(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    args = ["--no-config", "--files", "--hidden", "--glob=!**/SKILL.md", "--glob=!**/.git/**", "."]
    return (await run_rg(args, directory, SAMPLED_FILES, relative)).items
