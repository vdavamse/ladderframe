"""`Write`: create or overwrite a file, creating parent directories. Keeps an existing file's BOM.
Follows opencode's write tool."""

from __future__ import annotations

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ToolReturn

from ..core.deps import HarnessDeps
from ..core.permissions import PermissionDenied
from .base import tool
from .edit import count_changes, file_lock, split_bom, trim_diff, unified_diff, write_text
from .read import path_subject

DESCRIPTION = """Writes a file to the local filesystem.

Usage:
- This tool will overwrite the existing file if there is one at the provided path.
- If this is an existing file, you MUST use the Read tool first to read the file's contents. This tool will fail if you did not read the file first.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked."""


@tool(subject=path_subject, truncates=True, description=DESCRIPTION)
def Write(ctx: RunContext[HarnessDeps], content: str, filePath: str) -> str | ToolReturn:
    """Write a file.

    Args:
        content: The content to write to the file
        filePath: The absolute path to the file to write (must be absolute, not relative)
    """
    path = ctx.deps.resolve_path(filePath)
    try:  # a symlink can lead somewhere the rules deny
        ctx.deps.permissions.check("Write", str(path))
    except PermissionDenied as exc:
        return str(exc)
    if path.is_dir():
        raise ModelRetry(f"Path is a directory, not a file: {path}")

    with file_lock(path):
        exists = path.exists()
        # The old text only feeds the diff, so an undecodable file is still overwritten.
        source_bom, content_old = (
            split_bom(path.read_bytes().decode("utf-8", errors="replace")) if exists else (False, "")
        )
        next_bom, content_new = split_bom(content)
        write_text(path, content_new, source_bom or next_bom)

    diff = trim_diff(unified_diff(str(path), content_old, content_new))
    additions, deletions = count_changes(content_old, content_new)
    return ToolReturn(
        "Wrote file successfully.",
        metadata={
            "filepath": str(path),
            "exists": exists,
            "diff": diff,
            "additions": additions,
            "deletions": deletions,
        },
    )
