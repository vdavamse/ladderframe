from __future__ import annotations

import asyncio
import os
import signal

from pydantic import BaseModel
from pydantic_ai import RunContext

from ..core.deps import HarnessDeps
from ..utils.text import truncate
from .base import tool


class BashSettings(BaseModel):
    timeout: float = 120
    max_timeout: float = 600
    max_output_chars: int = 30_000
    shell: str = "bash"
    env: dict[str, str] = {}
    """Extra environment variables for every command."""


@tool(settings=BashSettings, subject="command")
async def Bash(
    ctx: RunContext[HarnessDeps], command: str, timeout: float | None = None, description: str | None = None
) -> str:
    """Execute a shell command in the working directory and return its combined stdout/stderr and exit code.

    Prefer the dedicated Read, Glob and Grep tools for reading and searching files.

    Args:
        command: The command to run.
        timeout: Timeout in seconds (capped by the configured maximum).
        description: A short description of what the command does, for logs.
    """
    settings = ctx.deps.settings("Bash", BashSettings)
    limit = min(timeout or settings.timeout, settings.max_timeout)
    env = {**os.environ, **settings.env, "AGENT_ROOT": str(ctx.deps.root.path)}
    process = await asyncio.create_subprocess_exec(
        settings.shell,
        "-c",
        command,
        cwd=ctx.deps.workdir,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), limit)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)  # the whole process group, not just the shell
        await process.wait()
        return f"Command timed out after {limit:g}s and was killed."
    output = truncate(stdout.decode(errors="replace"), settings.max_output_chars)
    return f"{output}\n[exit code: {process.returncode}]" if output else f"[exit code: {process.returncode}]"
