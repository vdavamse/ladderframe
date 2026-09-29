"""`Grep`: regex search over file contents via `rg --json`, grouped by file like opencode's grep tool."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from ..core.deps import HarnessDeps
from .base import tool
from .ripgrep import relative, run_rg

DESCRIPTION = """- Fast content search tool that works with any codebase size
- Searches file contents using regular expressions
- Supports full regex syntax (eg. "log.*Error", "function\\s+\\w+", etc.)
- Filter files by pattern with the include parameter (eg. "*.js", "*.{ts,tsx}")
- Returns file paths and line numbers with matching lines
- Use this tool when you need to find files containing specific patterns
- If you need to identify/count the number of matches within files, use the Bash tool with `rg` (ripgrep) directly. Do NOT use `grep`.
- When you are doing an open-ended search that may require multiple rounds of globbing and grepping, use the Agent tool instead"""

MAX_LINE_LENGTH = 2000


class GrepSettings(BaseModel):
    limit: int = 100


@dataclass
class Match:
    path: str
    line: int
    text: str


@tool(settings=GrepSettings, subject="pattern", truncates=True, description=DESCRIPTION)
async def Grep(ctx: RunContext[HarnessDeps], pattern: str, path: str | None = None, include: str | None = None) -> str:
    """Search file contents with a regular expression.

    Args:
        pattern: The regex pattern to search for in file contents
        path: The directory to search in. Defaults to the current working directory.
        include: File pattern to include in the search (e.g. "*.js", "*.{ts,tsx}")
    """
    if not pattern:
        raise ModelRetry("pattern is required")
    limit = ctx.deps.settings("Grep", GrepSettings).limit
    requested = ctx.deps.resolve_path(path) if path else ctx.deps.workdir_path
    if not requested.exists():
        raise ModelRetry(f"No such file or directory: {requested}")
    cwd = requested if requested.is_dir() else requested.parent

    args = ["--no-config", "--json", "--hidden", "--no-messages"]
    if include:
        args.append(f"--glob={include}")
    args += ["--glob=!**/.git/**", "--", pattern, "." if requested.is_dir() else requested.name]
    result = await run_rg(args, cwd, limit, _parse, pattern=pattern)
    matches = result.items

    if not matches:
        return "No files found"
    truncated = result.truncated
    output = [f"Found {len(matches)} matches{' (more matches available)' if truncated else ''}"]
    current = ""
    for match in matches:
        file = str((cwd / match.path).resolve())
        if file != current:
            if current:
                output.append("")
            current = file
            output.append(f"{file}:")
        output.append(f"  Line {match.line}: {match.text}")
    if truncated:
        output += ["", "(Results truncated. Consider using a more specific path or pattern.)"]
    return "\n".join(output)


def _parse(line: str) -> Match | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if record.get("type") != "match":
        return None
    data = record["data"]
    return Match(relative(_text(data.get("path"))), data.get("line_number") or 0, _clip(_text(data.get("lines"))))


def _text(obj: dict[str, str] | None) -> str:
    """rg --json gives `{"text": ...}`, or `{"bytes": <base64>}` for data that isn't valid UTF-8."""
    obj = obj or {}
    if "text" in obj:
        return obj["text"]
    return base64.b64decode(obj.get("bytes", "")).decode("utf-8", errors="replace")


def _clip(text: str) -> str:
    text = text.rstrip("\r\n")
    return text[:MAX_LINE_LENGTH] + "..." if len(text) > MAX_LINE_LENGTH else text
