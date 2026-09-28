from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry

from ladderframe.tools import BUILTIN_TOOLS, Bash, Glob, Grep, Read
from ladderframe.tools.grep import _python_grep


def test_builtin_names_match_claude_code() -> None:
    assert set(BUILTIN_TOOLS) == {"Read", "Bash", "Glob", "Grep", "WebFetch", "WebSearch", "Agent", "Skill"}


def test_read(ctx: SimpleNamespace) -> None:
    out = Read(ctx, "data/sample.txt")
    assert out.splitlines()[0] == "     1\tline one"
    assert "Showing lines 2-2 of 3" in Read(ctx, "data/sample.txt", offset=2, limit=1)
    with pytest.raises(ModelRetry):
        Read(ctx, "data/missing.txt")


def test_glob(ctx: SimpleNamespace) -> None:
    assert Glob(ctx, "**/*.txt").endswith("data/sample.txt")
    assert Glob(ctx, "**/*.nothing") == "No files found"


async def test_grep(ctx: SimpleNamespace) -> None:
    out = await Grep(ctx, "needle", path="data", output_mode="content")
    assert "sample.txt:3:needle here" in out
    fallback = _python_grep("needle", ctx.deps.workdir_path / "data", None, "files_with_matches", False, True, False)
    assert "sample.txt" in fallback


async def test_bash(ctx: SimpleNamespace) -> None:
    assert await Bash(ctx, "echo hi") == "hi\n\n[exit code: 0]"
    assert "exit code: 3" in await Bash(ctx, "exit 3")
    assert "timed out" in await Bash(ctx, "sleep 5", timeout=0.2)
