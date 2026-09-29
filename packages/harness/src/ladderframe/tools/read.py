"""`Read`: files (with line numbers), directories, images and PDFs. Output format follows opencode's read tool."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import BinaryContent, ToolReturn, ToolReturnPart

from ..core.deps import HarnessDeps
from .base import tool

DESCRIPTION = """Read a file or directory from the local filesystem. If the path does not exist, an error is returned.

Usage:
- The filePath parameter should be an absolute path.
- By default, this tool returns up to 2000 lines from the start of the file.
- The offset parameter is the line number to start from (1-indexed).
- To read later sections, call this tool again with a larger offset.
- Use the Grep tool to find specific content in large files or files with long lines.
- If you are unsure of the correct file path, use the Glob tool to look up filenames by glob pattern.
- Contents are returned with each line prefixed by its line number as `<line>: <content>`. For example, if a file has contents "foo\\n", you will receive "1: foo\\n". For directories, entries are returned one per line (without line numbers) with a trailing `/` for subdirectories.
- Any line longer than 2000 characters is truncated.
- Call this tool in parallel when you know there are multiple files you want to read.
- Avoid tiny repeated slices (30 line chunks). If you need more context, read a larger window.
- This tool can read image files and PDFs and return them as file attachments."""

SAMPLE_BYTES = 4096
IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
BINARY_EXTENSIONS = {
    ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".class", ".jar", ".war", ".7z", ".doc", ".docx", ".xls",
    ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".bin", ".dat", ".obj", ".o", ".a", ".lib", ".wasm",
    ".pyc", ".pyo",
}  # fmt: skip
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


class ReadSettings(BaseModel):
    default_limit: int = 2000
    max_line_length: int = 2000
    max_bytes: int = 50 * 1024
    """Stop reading once this many bytes of lines have been collected."""


@tool(settings=ReadSettings, subject="filePath", truncates=True, description=DESCRIPTION)
def Read(
    ctx: RunContext[HarnessDeps], filePath: str, offset: int | None = None, limit: int | None = None
) -> str | ToolReturn:
    """Read a file or directory.

    Args:
        filePath: The absolute path to the file or directory to read
        offset: The line number to start reading from (1-indexed)
        limit: The maximum number of lines to read (defaults to 2000)
    """
    settings = ctx.deps.settings("Read", ReadSettings)
    if (offset is not None and offset < 0) or (limit is not None and limit < 0):
        raise ModelRetry("offset and limit must be non-negative integers")
    path = ctx.deps.resolve_path(filePath)
    if not path.exists():
        raise ModelRetry(_not_found(path))

    if path.is_dir():
        return _read_directory(path, offset or 1, limit if limit is not None else settings.default_limit)

    sample = _sample(path)
    mime = _sniff_mime(sample, path)
    if mime in IMAGE_MIMES or mime == "application/pdf":
        message = "PDF read successfully" if mime == "application/pdf" else "Image read successfully"
        return ToolReturn(message, content=[BinaryContent(data=path.read_bytes(), media_type=mime)])
    if _is_binary(path, sample):
        raise ModelRetry(f"Cannot read binary file: {path}")

    start = offset or 1
    count = limit if limit is not None else settings.default_limit
    lines, total, more, capped = _read_lines(path, start, count, settings)
    if total < start and not (total == 0 and start == 1):
        raise ModelRetry(f"Offset {start} is out of range for this file ({total} lines)")

    output = f"<path>{path}</path>\n<type>file</type>\n<content>\n"
    output += "\n".join(f"{start + i}: {line}" for i, line in enumerate(lines))
    last = start + len(lines) - 1
    if capped:
        output += (
            f"\n\n(Output capped at {settings.max_bytes // 1024} KB. "
            f"Showing lines {start}-{last}. Use offset={last + 1} to continue.)"
        )
    elif more:
        output += f"\n\n(Showing lines {start}-{last} of {total}. Use offset={last + 1} to continue.)"
    else:
        output += f"\n\n(End of file - total {total} lines)"
    output += "\n</content>"

    instructions = _nearby_instructions(ctx, path)
    if instructions:
        output += "\n\n<system-reminder>\n" + "\n\n".join(instructions) + "\n</system-reminder>"
    return output


def _not_found(path: Path) -> str:
    base = path.name.lower()
    try:
        entries = sorted(p.name for p in path.parent.iterdir())
    except OSError:
        entries = []
    similar = [str(path.parent / e) for e in entries if base in e.lower() or e.lower() in base][:3]
    if similar:
        return f"File not found: {path}\n\nDid you mean one of these?\n" + "\n".join(similar)
    return f"File not found: {path}"


def _read_directory(path: Path, offset: int, limit: int) -> str:
    entries = sorted(
        (f"{p.name}/" if p.is_dir() else p.name for p in path.iterdir()), key=lambda n: (n.lower(), n.swapcase())
    )
    start = offset - 1
    shown = entries[start : start + limit]
    truncated = start + len(shown) < len(entries)
    footer = (
        f"\n(Showing {len(shown)} of {len(entries)} entries. "
        f"Use 'offset' parameter to read beyond entry {offset + len(shown)})"
        if truncated
        else f"\n({len(entries)} entries)"
    )
    return "\n".join(
        [f"<path>{path}</path>", "<type>directory</type>", "<entries>", "\n".join(shown), footer, "</entries>"]
    )


def _sample(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(SAMPLE_BYTES)


def _sniff_mime(sample: bytes, path: Path) -> str:
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        return "image/webp"
    if sample.startswith(b"%PDF-"):
        return "application/pdf"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _is_binary(path: Path, sample: bytes) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    if not sample:
        return False
    if 0 in sample:
        return True
    non_printable = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return non_printable / len(sample) > 0.3


def _read_lines(path: Path, start: int, limit: int, settings: ReadSettings) -> tuple[list[str], int, bool, bool]:
    """Lines from `start` (1-indexed): `(lines, lines seen, more after these, stopped at the byte cap)`."""
    lines: list[str] = []
    size = 0
    count = 0
    more = False
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for raw in handle:
            count += 1
            if count < start:
                continue
            if len(lines) >= limit:
                more = True
                continue
            line = raw.removesuffix("\n").removesuffix("\r")
            if len(line) > settings.max_line_length:
                line = line[: settings.max_line_length] + f"... (line truncated to {settings.max_line_length} chars)"
            line_size = len(line.encode("utf-8")) + (1 if lines else 0)
            if size + line_size > settings.max_bytes:
                return lines, count, True, True
            lines.append(line)
            size += line_size
    return lines, count, more, False


def _nearby_instructions(ctx: RunContext[HarnessDeps], path: Path) -> list[str]:
    """AGENTS.md / CLAUDE.md files between the file and the working directory, once per conversation."""
    root = ctx.deps.workdir_path.resolve()
    current = path.resolve().parent
    if root not in current.parents:
        return []
    seen = _already_loaded(getattr(ctx, "messages", None) or [])
    found: list[str] = []
    while current != root and root in current.parents:
        for name in INSTRUCTION_FILES:
            candidate = current / name
            if candidate.is_file():
                if candidate != path.resolve() and str(candidate) not in seen:
                    text = candidate.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        found.append(f"Instructions from: {candidate}\n{text}")
                break
        current = current.parent
    return found


def _already_loaded(messages: list[Any]) -> set[str]:
    loaded: set[str] = set()
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart) and part.tool_name == "Read" and isinstance(part.content, str):
                for line in part.content.splitlines():
                    if line.startswith("Instructions from: "):
                        loaded.add(line.removeprefix("Instructions from: "))
    return loaded
