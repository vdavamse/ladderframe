"""Pydantic models for `etc/ladderframe.yaml`."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(_Model):
    """Object form of `model`, borrowed from Claude Managed Agents (`{id, effort}`)."""

    id: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    """Passed through to pydantic-ai `model_settings` (temperature, max_tokens, ...)."""


class PermissionsConfig(_Model):
    """Claude Code style permission rules, e.g. `Bash(git *)`, `Read(/etc/**)`, `github_*`."""

    default: Literal["allow", "deny"] = "allow"
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class LimitsConfig(_Model):
    """Mapped onto pydantic-ai `UsageLimits` for every run."""

    request_limit: int | None = 50
    tool_calls_limit: int | None = None
    total_tokens_limit: int | None = None
    max_subagent_depth: int = 2


class TemporalConfig(_Model):
    address: str = "localhost:7233"
    namespace: str = "default"
    task_queue: str | None = None
    """Defaults to `ladderframe-<agent name>`."""
    session_idle_timeout: str = "7d"


class RuntimeConfig(_Model):
    executor: Literal["inline", "temporal"] = "inline"
    temporal: TemporalConfig = Field(default_factory=TemporalConfig)


class StorageConfig(_Model):
    """S3-compatible object storage (MinIO) for offloaded payloads and archived sessions."""

    endpoint: str | None = None
    bucket: str = "ladderframe"
    access_key: str | None = None
    secret_key: str | None = None
    region: str = "us-east-1"
    payload_prefix: str = "payloads/"
    payload_threshold_bytes: int = 256 * 1024
    """Temporal payloads at least this large are stored here instead of in workflow history."""
    session_prefix: str = "sessions/"


class ServerConfig(_Model):
    host: str = "127.0.0.1"
    port: int = 8080
    protocols: list[Literal["vercel-ai", "ag-ui", "web"]] = Field(default_factory=lambda: ["web"])
    auth: Literal["none", "api-key", "jwt"] = "none"
    api_keys: list[str] = Field(default_factory=list)


class HarnessConfig(_Model):
    name: str | None = None
    """Agent name. Defaults to the root directory name."""

    model: str | ModelConfig = "sonnet"
    model_aliases: dict[str, str] = Field(default_factory=dict)

    workdir: str = "."
    """Working directory for file and shell tools, relative to the process cwd."""

    tools: list[str] = Field(default_factory=list)
    """Enabled tools by name: built-ins, custom tools from `tools/`, and entry-point tools."""

    tool_settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-tool settings, keyed by tool name, e.g. `{Bash: {timeout: 120}}`."""

    tool_packages: list[str] = Field(default_factory=list)
    """Installed distributions whose `ladderframe.tools` entry points are enabled."""

    capabilities: list[str | dict[str, dict[str, Any]]] = Field(default_factory=list)
    """Capability names, or `{name: {option: value}}` mappings."""

    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    share: bool | str = True
    """`true`: auto-discover the monorepo `share/` dir; a path: use it; `false`: disabled."""

    claude_compat: bool = False
    """Also discover agents/skills from `.claude/` in the root and `~/.claude/`."""

    disable_skill_shell_execution: bool = False

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @field_validator("capabilities")
    @classmethod
    def _one_key_per_mapping(
        cls, value: list[str | dict[str, dict[str, Any]]]
    ) -> list[str | dict[str, dict[str, Any]]]:
        for item in value:
            if isinstance(item, dict) and len(item) != 1:
                raise ValueError(f"capability mapping must have exactly one key, got {list(item)}")
        return value

    @property
    def model_id(self) -> str:
        return self.model if isinstance(self.model, str) else self.model.id

    def resolve_model(self, model: str | None = None) -> str:
        """Resolve an alias (`sonnet`) or bare Claude id into a pydantic-ai model string."""
        name = model or self.model_id
        name = self.model_aliases.get(name, name)
        if ":" not in name and name.startswith("claude-"):
            name = f"anthropic:{name}"
        return name
