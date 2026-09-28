"""`mcp.yaml` -> pydantic-ai `MCPToolset`s.

The file format is pydantic-ai's `load_mcp_toolsets()` format (the `mcpServers` shape shared with
Claude Desktop / Claude Code / Cursor) written as YAML, and each entry is built the same way:
`command` -> stdio transport, `url` -> streamable HTTP (SSE when the path ends in `/sse`),
`${VAR}` / `${VAR:-default}` expansion, and `.prefixed(<server name>)`.

ladderframe adds optional keys that pydantic-ai's loader ignores:

    options: {...}     keyword arguments for `MCPToolset(...)` (auth, include_instructions, read_timeout, ...)
    prefix: str        tool-name prefix, default: the server name
    tools: [globs]     allowlist of the server's (unprefixed) tool names, via `.filtered(...)`
    enabled: bool      `false` keeps the definition but does not connect
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.toolsets import AbstractToolset

from ..config.loader import expand_env_vars, read_yaml

# MCPToolset arguments that can be expressed in YAML; the rest need Python objects.
_YAML_OPTIONS = {
    "max_retries",
    "tool_error_behavior",
    "prefer_tasks",
    "cache_tools",
    "cache_resources",
    "cache_prompts",
    "include_instructions",
    "include_return_schema",
    "log_level",
    "init_timeout",
    "read_timeout",
    "auth",
    "verify",
}


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")  # like pydantic-ai: unknown keys from other clients are ignored

    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    prefix: str | None = None
    tools: list[str] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _transport(self) -> MCPServerConfig:
        if not self.command and not self.url:
            raise ValueError("an MCP server needs either `command` or `url`")
        unknown = set(self.options) - _YAML_OPTIONS
        if unknown:
            raise ValueError(f"unsupported MCPToolset options: {sorted(unknown)}")
        return self


class MCPConfig(BaseModel):
    mcpServers: dict[str, MCPServerConfig] = Field(default_factory=dict)  # noqa: N815 — file format key


def load_mcp_config(files: list[Path], missing: set[str] | None = None) -> dict[str, MCPServerConfig]:
    """Merge `mcp.yaml` files by server name; earlier files (the agent root) win over later ones (share/)."""
    servers: dict[str, MCPServerConfig] = {}
    for path in files:
        if not path.exists():
            continue
        raw = read_yaml(path)
        entries = raw.get("mcpServers") or {}
        if not isinstance(entries, dict):
            raise ValueError(f"{path}: `mcpServers` must be a mapping")
        # Only enabled servers report undefined variables; a disabled one may lack its secrets.
        raw["mcpServers"] = {
            name: expand_env_vars(entry, missing if (entry or {}).get("enabled", True) else None)
            for name, entry in entries.items()
        }
        try:
            config = MCPConfig.model_validate(raw)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        for name, server in config.mcpServers.items():
            servers.setdefault(name, server)
    return servers


def build_toolset(name: str, server: MCPServerConfig) -> AbstractToolset[Any]:
    from pydantic_ai.mcp import MCPToolset

    if server.command:
        from fastmcp.client.transports import StdioTransport

        transport: Any = StdioTransport(
            command=server.command, args=list(server.args or []), env=server.env, cwd=server.cwd
        )
        toolset: AbstractToolset[Any] = MCPToolset(transport, id=name, **server.options)
    else:
        toolset = MCPToolset(server.url, id=name, headers=server.headers, **server.options)

    if server.tools is not None:
        patterns = server.tools
        toolset = toolset.filtered(lambda _ctx, tool_def: any(fnmatch.fnmatchcase(tool_def.name, p) for p in patterns))
    return toolset.prefixed(server.prefix or name)


def build_toolsets(servers: dict[str, MCPServerConfig], names: list[str] | None = None) -> list[AbstractToolset[Any]]:
    """Toolsets for the enabled servers (all of them, or just `names`)."""
    selected = servers if names is None else {n: servers[n] for n in names if n in servers}
    return [build_toolset(name, server) for name, server in selected.items() if server.enabled]
