"""Built-in tools: parameters and output follow opencode's tools (see each module's docstring)."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import BinaryContent, ToolReturn

from ladderframe import Runtime
from ladderframe.tools import BUILTIN_TOOLS, Bash, Glob, Grep, Read, WebFetch, truncate
from ladderframe.tools.agent import render_task
from ladderframe.tools.base import to_pydantic_tool
from ladderframe.tools.bash import _describe as _describe_bash
from ladderframe.tools.web_fetch import html_to_markdown, html_to_text
from ladderframe.tools.web_search import parse_response

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def work(runtime: Runtime, tmp_path: Path) -> SimpleNamespace:
    """A context whose working directory is an empty temporary directory."""
    deps = runtime.new_deps("test-session")
    deps.workdir = str(tmp_path)
    return SimpleNamespace(deps=deps, messages=[])


def test_builtin_names_match_claude_code() -> None:
    assert set(BUILTIN_TOOLS) == {"Read", "Bash", "Glob", "Grep", "WebFetch", "WebSearch", "Agent", "Skill"}


# ---------------------------------------------------------------- Read


def test_read_file(ctx: SimpleNamespace) -> None:
    path = ctx.deps.resolve_path("data/sample.txt")
    assert Read(ctx, "data/sample.txt") == (
        f"<path>{path}</path>\n<type>file</type>\n<content>\n"
        "1: line one\n2: line two\n3: needle here\n\n(End of file - total 3 lines)\n</content>"
    )
    partial = Read(ctx, str(path), offset=2, limit=1)
    assert "2: line two\n\n(Showing lines 2-2 of 3. Use offset=3 to continue.)" in partial


def test_read_errors(ctx: SimpleNamespace) -> None:
    with pytest.raises(ModelRetry, match="Did you mean one of these\\?\n.*sample.txt"):
        Read(ctx, "data/sample")
    with pytest.raises(ModelRetry, match="Offset 9 is out of range for this file \\(3 lines\\)"):
        Read(ctx, "data/sample.txt", offset=9)


def test_read_directory_binary_image_and_empty(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    (tmp_path / "a.png").write_bytes(PNG)
    (tmp_path / "empty.txt").write_text("")
    listing = Read(work, str(tmp_path))
    assert listing == (
        f"<path>{tmp_path}</path>\n<type>directory</type>\n<entries>\n"
        "a.png\nb.bin\nempty.txt\nsub/\n\n(4 entries)\n</entries>"
    )
    assert "(Showing 1 of 4 entries. Use 'offset' parameter to read beyond entry 2)" in Read(work, ".", limit=1)
    with pytest.raises(ModelRetry, match="Cannot read binary file"):
        Read(work, "b.bin")
    image = Read(work, "a.png")
    assert isinstance(image, ToolReturn) and image.return_value == "Image read successfully"
    assert isinstance(image.content, list) and isinstance(image.content[0], BinaryContent)
    assert "(End of file - total 0 lines)" in str(Read(work, "empty.txt"))


def test_read_caps_long_lines_and_bytes(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("x" * 2500 + "\n" + "\n".join("y" * 1000 for _ in range(100)))
    out = str(Read(work, "long.txt"))
    assert "... (line truncated to 2000 chars)" in out
    assert "(Output capped at 50 KB. Showing lines 1-" in out


def test_read_attaches_nested_agents_md_once(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("Use tabs.")
    (tmp_path / "pkg" / "f.py").write_text("x = 1\n")
    out = str(Read(work, "pkg/f.py"))
    assert (
        f"<system-reminder>\nInstructions from: {tmp_path / 'pkg' / 'AGENTS.md'}\nUse tabs.\n</system-reminder>" in out
    )
    (tmp_path / "AGENTS.md").write_text("root instructions are not attached")
    assert "root instructions" not in out


# ---------------------------------------------------------------- Glob / Grep


async def test_glob(ctx: SimpleNamespace) -> None:
    assert await Glob(ctx, "*.txt", path="data") == str(ctx.deps.resolve_path("data/sample.txt"))
    assert await Glob(ctx, "**/*.nothing") == "No files found"
    with pytest.raises(ModelRetry, match="glob path must be a directory"):
        await Glob(ctx, "*", path="data/sample.txt")


async def test_glob_truncates_at_limit(runtime: Runtime, work: SimpleNamespace, tmp_path: Path) -> None:
    runtime.config.tool_settings["Glob"] = {"limit": 2}
    for name in "abc":
        (tmp_path / f"{name}.py").write_text("")
    out = await Glob(work, "*.py")
    assert out.endswith(
        "\n\n(Results are truncated: showing first 2 results. Consider using a more specific path or pattern.)"
    )


async def test_grep(ctx: SimpleNamespace) -> None:
    path = ctx.deps.resolve_path("data/sample.txt")
    assert await Grep(ctx, "needle|two", path="data") == (
        f"Found 2 matches\n{path}:\n  Line 2: line two\n  Line 3: needle here"
    )
    assert await Grep(ctx, "needle", path="data/sample.txt", include="*.txt") == (
        f"Found 1 matches\n{path}:\n  Line 3: needle here"
    )
    assert await Grep(ctx, "needle", include="*.py") == "No files found"
    with pytest.raises(ModelRetry, match="regex parse error"):
        await Grep(ctx, "(unclosed")


async def test_grep_truncates_at_limit(runtime: Runtime, work: SimpleNamespace, tmp_path: Path) -> None:
    runtime.config.tool_settings["Grep"] = {"limit": 2}
    (tmp_path / "a.txt").write_text("hit\nhit\nhit\n")
    out = await Grep(work, "hit")
    assert out.startswith("Found 2 matches (more matches available)\n")
    assert out.endswith("\n\n(Results truncated. Consider using a more specific path or pattern.)")


# ---------------------------------------------------------------- Bash


async def test_bash(work: SimpleNamespace, tmp_path: Path) -> None:
    assert await Bash(work, "echo hi") == "hi\n"
    assert await Bash(work, "exit 3") == "(no output)"
    assert await Bash(work, "echo err >&2") == "err\n"
    (tmp_path / "sub").mkdir()
    assert await Bash(work, "pwd", workdir="sub") == f"{tmp_path / 'sub'}\n"
    timed_out = await Bash(work, "echo start; sleep 5", timeout=200)
    assert timed_out.startswith("start\n")
    assert "<shell_metadata>\nshell tool terminated command after exceeding timeout 200 ms." in timed_out
    with pytest.raises(ModelRetry, match="Invalid timeout value"):
        await Bash(work, "true", timeout=-1)


async def test_bash_keeps_tail_and_saves_full_output(runtime: Runtime, work: SimpleNamespace) -> None:
    runtime.config.tool_output.max_lines = 5
    out = await Bash(work, "seq 1 20")
    assert out.startswith("...output truncated...\n\nFull output saved to: ")
    assert out.endswith("\n\n17\n18\n19\n20\n")  # the last of the 5 lines is the empty one after "20\n"
    saved = Path(out.split("Full output saved to: ")[1].split("\n")[0])
    assert saved.read_text() == "".join(f"{i}\n" for i in range(1, 21))


async def test_bash_description_is_rendered(runtime: Runtime) -> None:
    ctx = SimpleNamespace(deps=runtime.new_deps())
    tool_def = await _describe_bash(ctx, to_pydantic_tool(Bash).tool_def)  # type: ignore[arg-type]
    assert "commands will time out after 120000ms" in tool_def.description
    assert f"Use `{runtime.root.tmp}` for temporary work" in tool_def.description
    assert set(tool_def.parameters_json_schema["properties"]) == {"command", "timeout", "workdir"}


# ---------------------------------------------------------------- truncation


def test_generic_truncation(work: SimpleNamespace) -> None:
    work.deps.config.tool_output.max_lines = 3
    result = truncate.truncate_output("\n".join(str(i) for i in range(10)), work.deps)
    assert result.truncated and result.output_path
    assert result.content.startswith("0\n1\n2\n\n...7 lines truncated...\n\nThe tool call succeeded")
    assert "Use Grep to search the full content or Read with offset/limit" in result.content
    assert Path(result.output_path).read_text().endswith("8\n9")
    assert truncate.truncate_output("short", work.deps).content == "short"
    assert "Use the Agent tool to have explore agent" in truncate.hint("/x", delegate=True)


# ---------------------------------------------------------------- web


def test_html_conversion() -> None:
    html = (
        "<html><head><style>p{}</style><script>x()</script></head><body><h1>Title</h1><ul><li>a</li></ul></body></html>"
    )
    assert html_to_markdown(html) == "# Title\n\n- a"
    assert html_to_text(html) == "Titlea"


def test_websearch_parses_sse_and_json() -> None:
    payload = '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"results"}]}}'
    assert parse_response(payload) == "results"
    assert parse_response(f"event: message\ndata: {payload}\n\n") == "results"
    assert parse_response("data: {}") is None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body, kind = {
            "/page": (b"<html><body><h2>Hi</h2><p>there</p></body></html>", "text/html; charset=utf-8"),
            "/img": (PNG, "image/png"),
        }.get(self.path, (b"missing", "text/plain"))
        self.send_response(200 if self.path in ("/page", "/img") else 404)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


async def test_webfetch(runtime: Runtime, ctx: SimpleNamespace) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(ModelRetry):  # private addresses are refused by default
            await WebFetch(ctx, f"{base}/page")
        runtime.config.tool_settings["WebFetch"] = {"allow_local_urls": True}
        assert await WebFetch(ctx, f"{base}/page") == "## Hi\n\nthere"
        assert await WebFetch(ctx, f"{base}/page", format="text") == "Hithere"
        assert "<h2>Hi</h2>" in str(await WebFetch(ctx, f"{base}/page", format="html"))
        image = await WebFetch(ctx, f"{base}/img")
        assert isinstance(image, ToolReturn) and image.return_value == "Image fetched successfully"
        with pytest.raises(ModelRetry, match="status code: 404"):
            await WebFetch(ctx, f"{base}/nope")
        with pytest.raises(ModelRetry, match="must start with http"):
            await WebFetch(ctx, "ftp://example.com")
    finally:
        await asyncio.to_thread(server.shutdown)


def test_render_task() -> None:
    assert render_task("task_1", "completed", "done") == (
        '<task id="task_1" state="completed">\n<task_result>\ndone\n</task_result>\n</task>'
    )
