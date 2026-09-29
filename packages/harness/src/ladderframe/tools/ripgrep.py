"""Runs ripgrep for Glob, Grep and Skill, like opencode's core ripgrep adapter.

ripgrep is a prerequisite: the `ripgrep` wheel (a dependency) installs `rg` next to the Python interpreter,
and a system `rg` on PATH is used otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Generic, TypeVar

from pydantic_ai import ModelRetry

T = TypeVar("T")

_INVALID_PATTERN = ("regex parse error", "error parsing glob", "unclosed character class", "invalid")


@cache
def rg_binary() -> str | None:
    name = "rg.exe" if sys.platform == "win32" else "rg"
    for directory in (sysconfig.get_path("scripts"), str(Path(sys.executable).parent)):
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("rg")


class RipgrepMissing(RuntimeError):
    pass


@dataclass
class RgResult(Generic[T]):
    items: list[T]
    truncated: bool


async def run_rg(
    args: list[str], cwd: Path, limit: int, parse: Callable[[str], T | None], *, pattern: str | None = None
) -> RgResult[T]:
    """Run `rg`, parse stdout line by line and stop after `limit` items (`truncated` if there were more)."""
    binary = rg_binary()
    if binary is None:
        raise RipgrepMissing(
            "ripgrep (rg) is required but was not found; install the `ripgrep` package or put rg on PATH"
        )
    process = await asyncio.create_subprocess_exec(
        binary,
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )
    assert process.stdout is not None and process.stderr is not None
    items: list[T] = []
    truncated = False
    try:
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            item = parse(line)
            if item is None:
                continue
            if len(items) >= limit:
                truncated = True
                break
            items.append(item)
    finally:
        if truncated and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
    code = await process.wait()
    if truncated:
        return RgResult(items, True)
    if pattern is not None and code == 2 and any(marker in stderr.lower() for marker in _INVALID_PATTERN):
        raise ModelRetry(stderr)
    if code not in (0, 1, 2):
        raise ModelRetry(stderr or f"ripgrep failed with code {code}")
    return RgResult([] if code == 1 else items, False)


def relative(line: str) -> str:
    while line.startswith(("./", ".\\")):
        line = line[2:]
    return line.lstrip("/\\").replace("\\", "/")
