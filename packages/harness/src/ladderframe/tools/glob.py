from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from ..core.deps import HarnessDeps
from .base import tool

_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


class GlobSettings(BaseModel):
    max_results: int = 1000


@tool(settings=GlobSettings, subject="path")
def Glob(ctx: RunContext[HarnessDeps], pattern: str, path: str | None = None) -> str:
    """Find files by glob pattern (e.g. `**/*.py`, `src/**/*.ts`), newest first.

    Args:
        pattern: The glob pattern to match files against.
        path: Directory to search in. Defaults to the working directory.
    """
    settings = ctx.deps.settings("Glob", GlobSettings)
    base = ctx.deps.resolve_path(path or ".")
    if not base.is_dir():
        raise ModelRetry(f"Not a directory: {base}")
    matches = [p for p in base.glob(pattern) if p.is_file() and not _SKIP_DIRS.intersection(p.relative_to(base).parts)]
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return "No files found"
    shown = matches[: settings.max_results]
    result = "\n".join(str(p) for p in shown)
    if len(matches) > len(shown):
        result += f"\n[{len(matches) - len(shown)} more results omitted; narrow the pattern.]"
    return result
