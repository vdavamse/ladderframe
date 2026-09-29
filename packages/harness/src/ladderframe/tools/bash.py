"""`Bash`: run a shell command. Prompt, parameters and output follow opencode's shell tool.

Output is stdout and stderr interleaved. Past `tool_output` limits only the tail is returned and the full
output is saved to a file under var/lib/tool-output/. The exit code is not part of the output. Timeouts
and aborts are reported in a trailing `<shell_metadata>` block.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path
from typing import IO

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from ..core.deps import HarnessDeps
from . import truncate
from .base import tool

DESCRIPTION = """Executes a given bash command in a persistent shell session with optional timeout, ensuring proper handling and security measures.

Be aware: OS: {os}, Shell: {shell}

All commands run in the current working directory by default. Use the `workdir` parameter if you need to run a command in a different directory. AVOID using `cd <directory> && <command>` patterns - use `workdir` instead.

Use `{tmp}` for temporary work outside the workspace. This directory has already been created, already exists, and is pre-approved for external directory access.

IMPORTANT: This tool is for terminal operations like git, npm, docker, etc. DO NOT use it for file operations (reading, writing, editing, searching, finding files) - use the specialized tools for this instead.

Before executing the command, please follow these steps:

1. Directory Verification:
   - If the command will create new directories or files, first use `ls` to verify the parent directory exists and is the correct location
   - For example, before running "mkdir foo/bar", first use `ls foo` to check that "foo" exists and is the intended parent directory

2. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., rm "path with spaces/file.txt")
   - Examples of proper quoting:
     - mkdir "/Users/name/My Documents" (correct)
     - mkdir /Users/name/My Documents (incorrect - will fail)
     - python "/path/with spaces/script.py" (correct)
     - python /path/with spaces/script.py (incorrect - will fail)
   - After ensuring proper quoting, execute the command.
   - Capture the output of the command.

Usage notes:
  - The command argument is required.
  - You can specify an optional timeout in milliseconds. If not specified, commands will time out after {timeout}ms.
  - If the output exceeds {max_lines} lines or {max_bytes} bytes, it will be truncated and the full output will be written to a file. You can use Read with offset/limit to read specific sections or Grep to search the full content. Do NOT use `head`, `tail`, or other truncation commands to limit output; the full output will already be captured to a file for more precise searching.

  - Avoid using Bash with the `find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, or `echo` commands, unless explicitly instructed or when these commands are truly necessary for the task. Instead, always prefer using the dedicated tools for these commands:
    - File search: Use Glob (NOT find or ls)
    - Content search: Use Grep (NOT grep or rg)
    - Read files: Use Read (NOT cat/head/tail)
    - Communication: Output text directly (NOT echo/printf)
  - When issuing multiple commands:
    - If the commands are independent and can run in parallel, make multiple bash tool calls in a single message. For example, if you need to run "git status" and "git diff", send a single message with two bash tool calls in parallel.
    - If the commands depend on each other and must run sequentially, use a single Bash call with '&&' to chain them together (e.g., `git add . && git commit -m "message" && git push`). For instance, if one operation must complete before another starts (like mkdir before cp, or git add before git commit), run these operations sequentially instead.
    - Use ';' only when you need to run commands sequentially but don't care if earlier commands fail
    - DO NOT use newlines to separate commands (newlines are ok in quoted strings)
  - AVOID using `cd <directory> && <command>`. Use the `workdir` parameter to change directories instead.
    <good-example>
    Use workdir="/foo/bar" with command: pytest tests
    </good-example>
    <bad-example>
    cd /foo/bar && pytest tests
    </bad-example>

# Git and GitHub
- Only commit, amend, push, or create PRs when explicitly requested.
- Before committing, inspect `git status`, `git diff`, and `git log --oneline -10`; stage only intended files and never commit secrets.
- Write a concise commit message that matches the repo style.
- Do not update git config, skip hooks, use interactive `-i`, force-push, or create empty commits unless explicitly requested.
- If a commit fails or hooks reject it, fix the issue and create a new commit; do not amend the failed commit.
- Before creating a PR, inspect status, diff, remote tracking, recent commits, and the diff from the base branch.
- Review all commits included in the PR, not just the latest commit.
- Use `gh` for GitHub tasks, including PRs, issues, checks, and releases; return the PR URL when done."""

_KILL_GRACE_SECONDS = 3
_DRAIN_SECONDS = 2
"""After the shell exits, how long to keep reading output held open by background children."""


class BashSettings(BaseModel):
    timeout_ms: int = 120_000
    """Default timeout when the model does not pass one."""
    max_timeout_ms: int | None = None
    """Upper bound for the model's `timeout`; none by default, as in opencode."""
    shell: str = "bash"
    env: dict[str, str] = {}
    """Extra environment variables for every command."""


async def _describe(ctx: RunContext[HarnessDeps], tool_def: ToolDefinition) -> ToolDefinition:
    settings = ctx.deps.settings("Bash", BashSettings)
    max_lines, max_bytes = truncate.limits(ctx.deps)
    description = DESCRIPTION.format(
        os=sys.platform,
        shell=Path(settings.shell).name,
        tmp=ctx.deps.root.tmp,
        timeout=settings.timeout_ms,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )
    return replace(tool_def, description=description)


@tool(settings=BashSettings, subject="command", truncates=True, prepare=_describe)
async def Bash(
    ctx: RunContext[HarnessDeps], command: str, timeout: int | None = None, workdir: str | None = None
) -> str:
    """Run a shell command.

    Args:
        command: The command to execute
        timeout: Optional timeout in milliseconds
        workdir: The working directory to run the command in. Defaults to the current directory. Use this instead of 'cd' commands.
    """
    settings = ctx.deps.settings("Bash", BashSettings)
    if timeout is not None and timeout <= 0:
        raise ModelRetry(f"Invalid timeout value: {timeout}. Timeout must be a positive number.")
    timeout_ms = timeout or settings.timeout_ms
    if settings.max_timeout_ms:
        timeout_ms = min(timeout_ms, settings.max_timeout_ms)
    cwd = ctx.deps.resolve_path(workdir) if workdir else ctx.deps.workdir_path
    if not cwd.is_dir():
        raise ModelRetry(f"workdir is not a directory: {cwd}")
    ctx.deps.root.tmp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **settings.env, "AGENT_ROOT": str(ctx.deps.root.path)}
    return await _run(ctx.deps, settings.shell, command, cwd, env, timeout_ms)


async def _run(deps: HarnessDeps, shell: str, command: str, cwd: Path, env: dict[str, str], timeout_ms: int) -> str:
    max_lines, max_bytes = truncate.limits(deps)
    keep = max_bytes * 2
    chunks: list[str] = []
    used = 0
    full = ""
    file: str | None = None
    sink: IO[str] | None = None
    cut = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def consume(text: str) -> None:
        nonlocal used, full, file, sink, cut
        if not text:
            return
        size = len(text.encode("utf-8"))
        chunks.append(text)
        used += size
        while used > keep and len(chunks) > 1:
            used -= len(chunks.pop(0).encode("utf-8"))
            cut = True
        if sink is not None:
            sink.write(text)
            return
        full += text
        if len(full.encode("utf-8")) > max_bytes:
            file = truncate.write_output(truncate.output_dir(deps), full)
            sink = open(file, "a", encoding="utf-8")  # noqa: SIM115 - closed in the finally below
            full = ""
            cut = True

    process = await asyncio.create_subprocess_exec(
        shell,
        "-c",
        command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    stdout = process.stdout

    async def pump() -> None:
        while data := await stdout.read(64 * 1024):
            consume(decoder.decode(data))

    reader = asyncio.create_task(pump())
    expired = False
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout_ms / 1000 + 0.1)
        except TimeoutError:
            expired = True
            await _kill(process)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), _DRAIN_SECONDS)
        consume(decoder.decode(b"", final=True))
    except asyncio.CancelledError:
        await _kill(process)
        raise
    finally:
        reader.cancel()
        if sink is not None:
            sink.close()

    raw = "".join(chunks)
    tail_text, tail_cut = _tail(raw, max_lines, max_bytes)
    if tail_cut:
        cut = True
        if file is None:
            file = truncate.write_output(truncate.output_dir(deps), raw)
    output = tail_text or "(no output)"
    if cut and file:
        output = f"...output truncated...\n\nFull output saved to: {file}\n\n{output}"
    if expired:
        output += (
            "\n\n<shell_metadata>\n"
            f"shell tool terminated command after exceeding timeout {timeout_ms} ms. If this command is expected "
            "to take longer and is not waiting for interactive input, retry with a larger timeout value in "
            "milliseconds.\n</shell_metadata>"
        )
    return output


async def _kill(process: asyncio.subprocess.Process) -> None:
    """SIGTERM the whole process group, then SIGKILL after a grace period."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), _KILL_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


def _tail(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool]:
    lines = text.split("\n")
    if len(lines) <= max_lines and len(text.encode("utf-8")) <= max_bytes:
        return text, False
    out: list[str] = []
    size = 0
    for line in reversed(lines):
        if len(out) >= max_lines:
            break
        encoded = line.encode("utf-8")
        line_size = len(encoded) + (1 if out else 0)
        if size + line_size > max_bytes:
            if not out:  # a single huge line: keep its last max_bytes bytes
                out.append(encoded[-max_bytes:].decode("utf-8", errors="ignore"))
            break
        out.append(line)
        size += line_size
    return "\n".join(reversed(out)), True
