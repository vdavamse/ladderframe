"""Built-in tools: parameters and output follow opencode's tools (see each module's docstring)."""

import asyncio
import os
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
from ladderframe.tools.edit import Edit, replace
from ladderframe.tools.web_fetch import html_to_markdown, html_to_text
from ladderframe.tools.web_search import parse_response
from ladderframe.tools.write import Write

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def work(runtime: Runtime, tmp_path: Path) -> SimpleNamespace:
    """A context whose working directory is an empty temporary directory."""
    deps = runtime.new_deps("test-session")
    deps.workdir = str(tmp_path)
    return SimpleNamespace(deps=deps, messages=[])


def test_builtin_names_match_claude_code() -> None:
    assert set(BUILTIN_TOOLS) == {
        "Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch", "Agent", "Skill"
    }  # fmt: skip


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


# ---------------------------------------------------------------- review fixes


def test_read_inside_a_temporal_activity(work: SimpleNamespace, tmp_path: Path) -> None:
    from pydantic_ai.durable_exec.temporal import TemporalRunContext

    from ladderframe.runtime.temporal.run_context import HarnessRunContext

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("Use tabs.")
    (tmp_path / "pkg" / "f.py").write_text("x = 1\n")
    # A plain TemporalRunContext has no `messages`: Read still works and includes the instructions.
    plain = TemporalRunContext.deserialize_run_context({}, deps=work.deps)
    assert "Use tabs." in str(Read(plain, "pkg/f.py"))  # type: ignore[arg-type]
    # HarnessRunContext carries the files already included, so they aren't repeated.
    loaded = [str(tmp_path / "pkg" / "AGENTS.md")]
    carried = HarnessRunContext.deserialize_run_context({"loaded_instructions": loaded}, deps=work.deps)
    assert "Use tabs." not in str(Read(carried, "pkg/f.py"))  # type: ignore[arg-type]


def test_read_rules_match_the_resolved_path(runtime: Runtime, work: SimpleNamespace, tmp_path: Path) -> None:
    from ladderframe.tools.base import get_meta, subject_of

    meta = get_meta(Read)
    runtime.config.permissions.deny = ["Read(/etc/**)"]
    permissions = work.deps.permissions
    depth = "../" * len(tmp_path.parts)
    for spelling in ("/etc/passwd", "//etc/passwd", "/./etc/passwd", f"{depth}etc/passwd"):
        assert not permissions.is_allowed("Read", subject_of(meta, {"filePath": spelling}, work.deps)), spelling
    assert permissions.is_allowed("Read", subject_of(meta, {"filePath": "notes.txt"}, work.deps))


def test_read_refuses_oversized_attachments(work: SimpleNamespace, tmp_path: Path) -> None:
    from ladderframe.tools.read import MAX_ATTACHMENT_BYTES

    (tmp_path / "big.png").write_bytes(PNG + b"\x00" * MAX_ATTACHMENT_BYTES)
    with pytest.raises(ModelRetry, match="too large to attach"):
        Read(work, "big.png")


async def test_read_line_numbers_agree_with_grep(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "log.txt").write_bytes(b"progress 10%\rprogress 100%\nNEEDLE\n")
    assert "Line 2: NEEDLE" in await Grep(work, "NEEDLE")
    assert "2: NEEDLE" in str(Read(work, "log.txt"))


async def test_glob_and_grep_at_exactly_the_limit_are_not_truncated(
    runtime: Runtime, work: SimpleNamespace, tmp_path: Path
) -> None:
    runtime.config.tool_settings["Glob"] = {"limit": 2}
    runtime.config.tool_settings["Grep"] = {"limit": 2}
    (tmp_path / "a.py").write_text("hit\n")
    (tmp_path / "b.py").write_text("hit\n")
    assert "truncated" not in await Glob(work, "*.py")
    out = await Grep(work, "hit")
    assert "truncated" not in out and "more matches" not in out


async def test_grep_decodes_non_utf8_lines(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "latin1.txt").write_bytes("caf\xe9 NEEDLE\n".encode("latin-1"))
    assert "Line 1: caf� NEEDLE" in await Grep(work, "NEEDLE")


async def test_grep_skips_lines_over_the_stream_limit(
    work: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ladderframe.tools import ripgrep

    monkeypatch.setattr(ripgrep, "STREAM_LIMIT", 64 * 1024)
    (tmp_path / "big.json").write_text("NEEDLE" + "x" * 200_000)
    (tmp_path / "small.txt").write_text("NEEDLE small\n")
    out = await Grep(work, "NEEDLE")
    assert "Line 1: NEEDLE small" in out and "big.json" not in out


def test_truncation_keeps_part_of_a_single_long_line() -> None:
    preview, removed, unit = truncate.cut("x" * 60_000, 2000, 51_200) or ("", 0, "")
    assert preview == "x" * 51_200 and removed == 60_000 - 51_200 and unit == "bytes"
    tail, _, _ = truncate.cut("a" * 10 + "b" * 100, 10, 50, direction="tail") or ("", 0, "")
    assert tail == "b" * 50


async def test_websearch_errors_do_not_leak_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2

    from ladderframe.tools import web_search

    def refuse(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, request=request)

    original = httpx2.AsyncClient

    def client(**kwargs: object) -> httpx2.AsyncClient:
        return original(transport=httpx2.MockTransport(refuse), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(web_search.httpx2, "AsyncClient", client)
    with pytest.raises(ModelRetry) as excinfo:
        await web_search.call_mcp("https://mcp.exa.ai/mcp?exaApiKey=SECRET123", "web_search_exa", {}, 5)
    assert "SECRET123" not in str(excinfo.value) and "429" in str(excinfo.value)


async def test_webfetch_timeout_is_at_least_one_second(
    runtime: Runtime, ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ladderframe.tools import web_fetch

    seen: list[int] = []

    async def download(url: str, headers: dict[str, str], seconds: int, settings: object) -> object:
        seen.append(seconds)
        raise ModelRetry("stop")

    monkeypatch.setattr(web_fetch, "_download", download)
    for timeout in (0.5, -5, 500):
        with pytest.raises(ModelRetry):
            await WebFetch(ctx, "https://example.com", timeout=timeout)
    assert seen == [1, 1, 120]


def test_read_checks_the_symlink_target(runtime: Runtime, work: SimpleNamespace, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key.txt").write_text("hunter2")
    (tmp_path / "link.txt").symlink_to(secret / "key.txt")
    runtime.config.permissions.deny = [f"Read({secret}/**)"]
    out = str(Read(work, "link.txt"))
    assert "Permission denied" in out and "hunter2" not in out


# ---------------------------------------------------------------- Write / Edit


def test_write_creates_and_overwrites(work: SimpleNamespace, tmp_path: Path) -> None:
    created = Write(work, "a\nb\n", "deep/new.txt")
    assert isinstance(created, ToolReturn) and created.return_value == "Wrote file successfully."
    assert (tmp_path / "deep" / "new.txt").read_text() == "a\nb\n"
    assert created.metadata["exists"] is False and created.metadata["additions"] == 2
    replaced = Write(work, "a\nc\n", str(tmp_path / "deep" / "new.txt"))
    assert isinstance(replaced, ToolReturn) and replaced.metadata["exists"] is True
    assert "-b\n+c" in replaced.metadata["diff"]
    with pytest.raises(ModelRetry, match="Path is a directory"):
        Write(work, "x", "deep")


def test_write_keeps_the_bom(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "bom.txt").write_bytes("\ufeffold".encode())
    Write(work, "new", "bom.txt")
    assert (tmp_path / "bom.txt").read_bytes() == "\ufeffnew".encode()


def test_edit_replaces_a_unique_match(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("def f():\n    return 1\n")
    result = Edit(work, "f.py", "return 1", "return 2")
    assert isinstance(result, ToolReturn) and result.return_value == "Edit applied successfully."
    assert (tmp_path / "f.py").read_text() == "def f():\n    return 2\n"
    assert result.metadata["additions"] == 1 and result.metadata["deletions"] == 1
    assert "-    return 1\n+    return 2" in result.metadata["diff"]
    (tmp_path / "g.py").write_text("    a = 1\n    b = 2\n")
    trimmed = Edit(work, "g.py", "b = 2", "b = 3")
    assert isinstance(trimmed, ToolReturn) and " a = 1\n-b = 2\n+b = 3" in trimmed.metadata["diff"]


def test_edit_errors(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x = 1\nx = 1\n")
    with pytest.raises(ModelRetry, match="identical"):
        Edit(work, "f.txt", "x", "x")
    with pytest.raises(ModelRetry, match="Found multiple matches"):
        Edit(work, "f.txt", "x = 1", "x = 2")
    with pytest.raises(ModelRetry, match="Could not find oldString"):
        Edit(work, "f.txt", "y = 1", "y = 2")
    with pytest.raises(ModelRetry, match="oldString cannot be empty"):
        Edit(work, "f.txt", "", "z")
    with pytest.raises(ModelRetry, match="not found"):
        Edit(work, "missing.txt", "a", "b")
    Edit(work, "f.txt", "x = 1", "x = 2", replaceAll=True)
    assert (tmp_path / "f.txt").read_text() == "x = 2\nx = 2\n"


def test_edit_with_empty_old_string_creates_the_file(work: SimpleNamespace, tmp_path: Path) -> None:
    Edit(work, "new/file.txt", "", "hello\n")
    assert (tmp_path / "new" / "file.txt").read_text() == "hello\n"


def test_edit_keeps_crlf_and_bom(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "win.txt").write_bytes("\ufeffone\r\ntwo\r\nthree\r\n".encode())
    Edit(work, "win.txt", "one\ntwo", "uno\ndos")
    assert (tmp_path / "win.txt").read_bytes() == "\ufeffuno\r\ndos\r\nthree\r\n".encode()


def test_edit_refuses_non_utf8(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")
    with pytest.raises(ModelRetry, match="not a UTF-8 text file"):
        Edit(work, "latin.txt", "caf", "bar")


def test_replace_fallback_matchers() -> None:
    code = "def f():\n    if x:\n        return 1\n    return 2\n"
    # wrong indentation: the line-trimmed matcher finds the whole lines, which newString replaces as given
    assert replace(code, "if x:\n    return 1", "    if y:\n        return 3") == (
        "def f():\n    if y:\n        return 3\n    return 2\n"
    )
    # collapsed whitespace
    assert replace("a  =   1\n", "a = 1", "a = 2") == "a = 2\n"
    # escaped newlines from a model that double-escaped
    assert replace('s = "x"\ny\n', 's = \\"x\\"', 's = "z"') == 's = "z"\ny\n'
    # a block whose middle line differs slightly is found by its first and last lines
    block = "start {\n    alpha_value = 1\n}\n"
    assert replace(block, "start {\n    alpha_valu = 1\n}", "start {}") == "start {}\n"
    # literal replacement: no `$&`-style substitution
    assert replace("a\n", "a", "$&") == "$&\n"


def test_replace_refuses_a_disproportionate_match() -> None:
    # the block anchors match one line further apart than oldString, and that extra line is huge
    content = "first\nm1\nm2\n" + "z" * 700 + "\nlast\n"
    with pytest.raises(ValueError, match="much larger than oldString"):
        replace(content, "first\nm1\nm2\nlast", "x")


def test_write_and_edit_rules_match_the_resolved_path(runtime: Runtime, work: SimpleNamespace, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "f.txt").write_text("a\n")
    (tmp_path / "link").symlink_to(secret)
    runtime.config.permissions.deny = [f"Edit({secret}/**)", f"Write({secret}/**)"]
    assert "Permission denied" in str(Edit(work, "link/f.txt", "a", "b"))
    assert "Permission denied" in str(Write(work, "b", "link/f.txt"))
    assert (secret / "f.txt").read_text() == "a\n"


def test_replace_other_fallback_matchers() -> None:
    from ladderframe.tools.edit import (
        context_aware_replacer,
        indentation_flexible_replacer,
        trimmed_boundary_replacer,
    )

    # Called directly: in `replace` an earlier, looser matcher usually finds these first.
    nested = "class A:\n    def f(self):\n        pass\n"
    assert list(indentation_flexible_replacer(nested, "def f(self):\n    pass")) == ["    def f(self):\n        pass"]
    assert list(trimmed_boundary_replacer("x = 1\ny = 2\n", "\n\ny = 2\n\n")) == ["y = 2"]
    block = "begin\n  one\n  two\n  three\nend\n"
    assert list(context_aware_replacer(block, "begin\n  one\n  TWO\n  three\nend")) == [block.removesuffix("\n")]
    assert list(context_aware_replacer(block, "begin\n  ONE\n  TWO\n  three\nend")) == []
    # block anchor with several candidates: the most similar middle wins
    two = "if a:\n    alpha = 1\nfi\nif a:\n    zzzzz = 9\nfi\n"
    assert replace(two, "if a:\n    alpha = 2\nfi", "done") == "done\nif a:\n    zzzzz = 9\nfi\n"


def test_block_anchor_skips_scoring_very_long_lines() -> None:
    import time

    content = "start\n" + "x" * 20_000 + "\nend\n"
    begin = time.monotonic()
    with pytest.raises(ValueError, match="Could not find oldString"):
        replace(content, "start\n" + "y" * 20_000 + "\nend", "z")
    assert time.monotonic() - begin < 1


def test_diff_and_counts_split_on_newlines_only() -> None:
    import time

    from ladderframe.tools.edit import count_changes, unified_diff

    diff = unified_diff("f", "a\x0cb\nc\n", "a\x0cb\nd\n")
    assert "No newline" not in diff and " a\x0cb\n-c\n+d\n" in diff
    assert count_changes(unified_diff("f", "a\rb\nc\n", "a\rb\nd\n")) == (1, 1)
    assert count_changes(unified_diff("f", "x\n", "+++y\n--- z\n")) == (2, 1)  # not mistaken for headers
    similar = "\n".join(f"value_{i} = compute({i}, alpha)" for i in range(2000))
    unique = "\n".join(f"u{i}" for i in range(1000))
    repeated = "\n".join(["}", ""] * 1000) + "\n" + unique  # slow for a non-autojunk SequenceMatcher
    begin = time.monotonic()
    assert count_changes(unified_diff("f", similar, similar.replace("alpha", "beta"))) == (2000, 2000)
    count_changes(unified_diff("f", repeated, "\n".join(["}", "", "a"] * 1000)))
    assert time.monotonic() - begin < 5  # difflib.ndiff took minutes here


def test_write_and_edit_report_os_errors(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x\n")
    with pytest.raises(ModelRetry, match="Cannot write"):
        Write(work, "y", "main.py/new.txt")
    with pytest.raises(ModelRetry, match="Cannot write"):
        Edit(work, "main.py/new.txt", "", "y")


def test_write_over_a_non_utf8_file(work: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")
    Write(work, "cafe\n", "latin.txt")
    assert (tmp_path / "latin.txt").read_text() == "cafe\n"


def test_concurrent_edits_of_one_file_keep_every_change(work: SimpleNamespace, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    (tmp_path / "many.txt").write_text("".join(f"line {i}\n" for i in range(20)))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: Edit(work, "many.txt", f"line {i}\n", f"done {i}\n"), range(20)))
    assert (tmp_path / "many.txt").read_text() == "".join(f"done {i}\n" for i in range(20))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_write_and_edit_report_unsearchable_directories(work: SimpleNamespace, tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "f.txt").write_text("a\n")
    locked.chmod(0)
    try:
        with pytest.raises(ModelRetry, match="Cannot access"):
            Edit(work, "locked/f.txt", "a", "b")
        with pytest.raises(ModelRetry, match="Cannot access"):
            Write(work, "b", "locked/f.txt")
    finally:
        locked.chmod(0o755)
