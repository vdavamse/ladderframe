from pathlib import Path

from ladderframe.mcp import build_toolsets, load_mcp_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_and_merge(tmp_path: Path) -> None:
    override = tmp_path / "mcp.yaml"
    override.write_text("mcpServers:\n  remote:\n    url: http://override/mcp\n")
    missing: set[str] = set()
    servers = load_mcp_config([override, FIXTURES / "mcp.yaml"], missing)
    assert servers["remote"].url == "http://override/mcp"  # first file wins
    assert servers["local"].tools == ["read_*"]
    assert not servers["offline"].enabled
    assert missing == set()  # the disabled server's variable is not reported


def test_build_toolsets_skips_disabled() -> None:
    servers = load_mcp_config([FIXTURES / "mcp.yaml"])
    toolsets = build_toolsets(servers)
    assert len(toolsets) == 2
    assert [ts.prefix for ts in toolsets] == ["local", "rem"]  # type: ignore[attr-defined]
    assert build_toolsets(servers, ["remote"])[0].prefix == "rem"  # type: ignore[attr-defined]
