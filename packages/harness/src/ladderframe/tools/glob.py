"""`Glob`: file paths matching a glob pattern, via `rg --files` (respects .gitignore, skips hidden files)."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from ..core.deps import HarnessDeps
from .base import tool
from .ripgrep import relative, run_rg

DESCRIPTION = """- Fast file pattern matching tool that works with any codebase size
- Supports glob patterns like "**/*.js" or "src/**/*.ts"
- Returns matching file paths
- Use this tool when you need to find files by name patterns
- When you are doing an open-ended search that may require multiple rounds of globbing and grepping, use the Agent tool instead
- You have the capability to call multiple tools in a single response. It is always better to speculatively perform multiple searches as a batch that are potentially useful."""


class GlobSettings(BaseModel):
    limit: int = 100


@tool(settings=GlobSettings, subject="pattern", truncates=True, description=DESCRIPTION)
async def Glob(ctx: RunContext[HarnessDeps], pattern: str, path: str | None = None) -> str:
    """Find files by glob pattern.

    Args:
        pattern: The glob pattern to match files against
        path: The directory to search in. If not specified, the current working directory will be used. IMPORTANT: Omit this field to use the default directory. DO NOT enter "undefined" or "null" - simply omit it for the default behavior. Must be a valid directory path if provided.
    """
    limit = ctx.deps.settings("Glob", GlobSettings).limit
    search = ctx.deps.resolve_path(path) if path else ctx.deps.workdir_path
    if search.is_file():
        raise ModelRetry(f"glob path must be a directory: {search}")
    if not search.is_dir():
        raise ModelRetry(f"No such directory: {search}")

    args = ["--no-config", "--files", f"--glob={pattern}", "--glob=!**/.git/**", "."]
    files = (await run_rg(args, search, limit, relative, pattern=pattern)).items

    if not files:
        return "No files found"
    output = [str((search / f).resolve()) for f in files]
    if len(files) == limit:
        output += [
            "",
            f"(Results are truncated: showing first {limit} results. Consider using a more specific path or pattern.)",
        ]
    return "\n".join(output)
