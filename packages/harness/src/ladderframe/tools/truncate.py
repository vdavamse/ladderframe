"""Tool output truncation, as in opencode.

Output over `tool_output.max_lines` lines or `tool_output.max_bytes` bytes is cut to a preview, and the
full text is saved under `var/lib/tool-output/` with a hint telling the model how to inspect it. Read,
Glob, Grep and Bash bound their own output; every other tool's text result goes through `truncate_output`.
Saved files are removed after seven days.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..core.deps import HarnessDeps

RETENTION_SECONDS = 7 * 24 * 3600
_CLEANUP_INTERVAL = 3600
_last_cleanup: dict[str, float] = {}


@dataclass
class Truncated:
    content: str
    truncated: bool
    output_path: str | None = None


def output_dir(deps: HarnessDeps) -> Path:
    return deps.root.var_lib / "tool-output"


def limits(deps: HarnessDeps) -> tuple[int, int]:
    config = deps.config.tool_output
    return config.max_lines, config.max_bytes


def write_output(directory: Path, text: str) -> str:
    """Save a full tool output and return its path. Also prunes files older than the retention period."""
    directory.mkdir(parents=True, exist_ok=True)
    _cleanup(directory)
    path = directory / f"tool_{time.time_ns():020d}{uuid.uuid4().hex[:8]}"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _cleanup(directory: Path) -> None:
    now = time.time()
    key = str(directory)
    if now - _last_cleanup.get(key, 0) < _CLEANUP_INTERVAL:
        return
    _last_cleanup[key] = now
    for entry in directory.glob("tool_*"):
        try:
            if now - entry.stat().st_mtime > RETENTION_SECONDS:
                entry.unlink()
        except OSError:
            pass


def hint(path: str, delegate: bool) -> str:
    if delegate:
        return (
            f"The tool call succeeded but the output was truncated. Full output saved to: {path}\n"
            "Use the Agent tool to have explore agent process this file with Grep and Read (with offset/limit). "
            "Do NOT read the full file yourself - delegate to save context."
        )
    return (
        f"The tool call succeeded but the output was truncated. Full output saved to: {path}\n"
        "Use Grep to search the full content or Read with offset/limit to view specific sections."
    )


def cut(
    text: str, max_lines: int, max_bytes: int, direction: Literal["head", "tail"] = "head"
) -> tuple[str, int, str] | None:
    """Return `(preview, removed, unit)` when `text` is over the limits, else None."""
    lines = text.split("\n")
    total_bytes = len(text.encode("utf-8"))
    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return None
    out: list[str] = []
    size = 0
    hit_bytes = False
    ordered = lines if direction == "head" else reversed(lines)
    for line in ordered:
        if len(out) >= max_lines:
            break
        line_size = len(line.encode("utf-8")) + (1 if out else 0)
        if size + line_size > max_bytes:
            hit_bytes = True
            break
        out.append(line)
        size += line_size
    if direction == "tail":
        out.reverse()
    removed = total_bytes - size if hit_bytes else len(lines) - len(out)
    return "\n".join(out), removed, "bytes" if hit_bytes else "lines"


def render(preview: str, removed: int, unit: str, path: str, delegate: bool, direction: str = "head") -> str:
    note = hint(path, delegate)
    if direction == "head":
        return f"{preview}\n\n...{removed} {unit} truncated...\n\n{note}"
    return f"...{removed} {unit} truncated...\n\n{note}\n\n{preview}"


def truncate_output(
    text: str, deps: HarnessDeps, *, delegate: bool = False, direction: Literal["head", "tail"] = "head"
) -> Truncated:
    """Cut `text` to the configured limits, saving the full text to a file when it was cut."""
    max_lines, max_bytes = limits(deps)
    result = cut(text, max_lines, max_bytes, direction)
    if result is None:
        return Truncated(text, False)
    preview, removed, unit = result
    path = write_output(output_dir(deps), text)
    return Truncated(render(preview, removed, unit, path, delegate, direction), True, path)


def can_delegate(deps: HarnessDeps, agent_tool_enabled: bool) -> bool:
    """Whether the truncation hint should suggest handing the file to a sub-agent."""
    if not agent_tool_enabled or deps.depth >= deps.config.limits.max_subagent_depth:
        return False
    if deps.permissions.tool_blocked("Agent"):
        return False
    names = set(deps.runtime.subagents)
    if deps.allowed_subagents is not None:
        names &= set(deps.allowed_subagents)
    return bool(names)
