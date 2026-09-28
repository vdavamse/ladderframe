from __future__ import annotations

import asyncio
import fnmatch
import re
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from ..core.deps import HarnessDeps
from ..utils.text import truncate
from .base import tool

OutputMode = Literal["content", "files_with_matches", "count"]


class GrepSettings(BaseModel):
    head_limit: int = 250
    max_output_chars: int = 30_000
    timeout: float = 60


@tool(settings=GrepSettings, subject="path")
async def Grep(
    ctx: RunContext[HarnessDeps],
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    type: str | None = None,
    output_mode: OutputMode = "files_with_matches",
    case_insensitive: bool = False,
    line_numbers: bool = True,
    context: int | None = None,
    multiline: bool = False,
    head_limit: int | None = None,
) -> str:
    """Search file contents with a regular expression (ripgrep syntax).

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search. Defaults to the working directory.
        glob: Only search files matching this glob, e.g. `*.py` or `*.{ts,tsx}`.
        type: Only search files of this ripgrep type, e.g. `py`, `js`, `rust`.
        output_mode: `content` shows matching lines, `files_with_matches` lists files, `count` counts matches per file.
        case_insensitive: Case-insensitive search.
        line_numbers: Show line numbers in `content` mode.
        context: Lines of context around each match in `content` mode.
        multiline: Allow patterns to span lines.
        head_limit: Maximum number of output lines.
    """
    settings = ctx.deps.settings("Grep", GrepSettings)
    target = ctx.deps.resolve_path(path or ".")
    if not target.exists():
        raise ModelRetry(f"Path does not exist: {target}")
    limit = head_limit or settings.head_limit

    if shutil.which("rg"):
        output = await _ripgrep(
            pattern, target, glob, type, output_mode, case_insensitive, line_numbers, context, multiline, settings
        )
    else:
        output = _python_grep(pattern, target, glob, output_mode, case_insensitive, line_numbers, multiline)

    lines = output.splitlines()
    if not lines:
        return "No matches found"
    result = "\n".join(lines[:limit])
    if len(lines) > limit:
        result += f"\n[{len(lines) - limit} more lines omitted; refine the pattern or raise head_limit.]"
    return truncate(result, settings.max_output_chars)


async def _ripgrep(
    pattern: str,
    target: Path,
    glob: str | None,
    type_: str | None,
    mode: OutputMode,
    case_insensitive: bool,
    line_numbers: bool,
    context: int | None,
    multiline: bool,
    settings: GrepSettings,
) -> str:
    args = ["rg", "--color=never", "--no-heading"]
    if mode == "files_with_matches":
        args.append("--files-with-matches")
    elif mode == "count":
        args.append("--count")
    else:
        if line_numbers:
            args.append("--line-number")
        if context:
            args += ["--context", str(context)]
    if case_insensitive:
        args.append("--ignore-case")
    if multiline:
        args += ["--multiline", "--multiline-dotall"]
    if glob:
        args += ["--glob", glob]
    if type_:
        args += ["--type", type_]
    args += ["--regexp", pattern, str(target)]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), settings.timeout)
    if process.returncode not in (0, 1):
        raise ModelRetry(f"ripgrep failed: {stderr.decode(errors='replace').strip()}")
    return stdout.decode(errors="replace")


_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}


def _python_grep(
    pattern: str,
    target: Path,
    glob: str | None,
    mode: OutputMode,
    case_insensitive: bool,
    line_numbers: bool,
    multiline: bool,
) -> str:
    """Fallback when `rg` is not installed (the boot script installs it)."""
    flags = (re.IGNORECASE if case_insensitive else 0) | (re.MULTILINE | re.DOTALL if multiline else 0)
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ModelRetry(f"Invalid regular expression: {exc}") from exc
    files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
    out: list[str] = []
    for file in files:
        if _SKIP_DIRS.intersection(file.parts) or (glob and not fnmatch.fnmatch(file.name, glob)):
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if mode == "files_with_matches":
            if regex.search(text):
                out.append(str(file))
        elif mode == "count":
            if n := len(regex.findall(text)):
                out.append(f"{file}:{n}")
        else:
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    out.append(f"{file}:{number}:{line}" if line_numbers else f"{file}:{line}")
    return "\n".join(out)
