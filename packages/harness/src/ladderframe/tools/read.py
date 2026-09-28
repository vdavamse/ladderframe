from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from ..core.deps import HarnessDeps
from .base import tool


class ReadSettings(BaseModel):
    max_lines: int = 2000
    max_line_length: int = 2000


@tool(settings=ReadSettings, subject="file_path")
def Read(ctx: RunContext[HarnessDeps], file_path: str, offset: int | None = None, limit: int | None = None) -> str:
    """Read a text file from the local filesystem. Output uses `cat -n` format with 1-based line numbers.

    Args:
        file_path: Absolute path, or a path relative to the working directory.
        offset: Line number to start reading from (1-based). Only needed for large files.
        limit: Number of lines to read. Defaults to the configured maximum.
    """
    settings = ctx.deps.settings("Read", ReadSettings)
    path = ctx.deps.resolve_path(file_path)
    if not path.exists():
        raise ModelRetry(f"File does not exist: {path}")
    if path.is_dir():
        raise ModelRetry(f"{path} is a directory; use Glob or Bash `ls` to list it.")

    start = max((offset or 1) - 1, 0)
    count = min(limit or settings.max_lines, settings.max_lines)
    lines: list[str] = []
    total = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            total = index + 1
            if index < start:
                continue
            if len(lines) >= count:
                continue
            line = line.rstrip("\n")
            if len(line) > settings.max_line_length:
                line = line[: settings.max_line_length] + " [line truncated]"
            lines.append(f"{index + 1:>6}\t{line}")

    if total == 0:
        return f"<system-reminder>{path} exists but is empty.</system-reminder>"
    body = "\n".join(lines)
    shown_end = start + len(lines)
    if shown_end < total:
        body += f"\n\n[Showing lines {start + 1}-{shown_end} of {total}. Use offset/limit to read more.]"
    return body
