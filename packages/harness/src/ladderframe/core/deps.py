"""Dependencies passed to every tool via `RunContext[HarnessDeps]`.

`HarnessDeps` holds only plain data, so pydantic can serialize it into Temporal activities. The
`Runtime` (config, discovered agents/skills, MCP servers) is looked up from a process-wide registry
by agent root, which the CLI, the server and the Temporal worker populate at startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from .permissions import Permissions

if TYPE_CHECKING:
    from ..config import HarnessConfig
    from ..runtime.runtime import Runtime
    from .rootfs import AgentRoot

SettingsT = TypeVar("SettingsT", bound=BaseModel)


@dataclass
class HarnessDeps:
    root_path: str
    workdir: str
    session_id: str
    depth: int = 0
    """0 for the main agent, +1 for each level of sub-agent."""
    allowed_subagents: list[str] | None = None
    """From `Agent(a, b)` in a tools list; `None` means every discovered sub-agent."""
    grants: list[str] = field(default_factory=list)
    """Temporary allow rules from invoked skills' `allowed-tools`, for the rest of this run."""
    restrictions: list[str] = field(default_factory=list)
    """Temporary deny rules from invoked skills' `disallowed-tools`, for the rest of this run."""

    @property
    def runtime(self) -> Runtime:
        from ..runtime.registry import get_runtime

        return get_runtime(self.root_path)

    @property
    def root(self) -> AgentRoot:
        return self.runtime.root

    @property
    def config(self) -> HarnessConfig:
        return self.runtime.config

    @property
    def permissions(self) -> Permissions:
        rules = self.config.permissions
        permissions = Permissions.from_config(rules.default, rules.allow, rules.deny)
        permissions.grant(self.grants)
        permissions.restrict(self.restrictions)
        return permissions

    @property
    def workdir_path(self) -> Path:
        return Path(self.workdir)

    def settings(self, tool_name: str, model: type[SettingsT]) -> SettingsT:
        """Validated per-tool settings from `tool_settings.<tool_name>` in the config."""
        return model.model_validate(self.config.tool_settings.get(tool_name, {}))

    def resolve_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        return (candidate if candidate.is_absolute() else self.workdir_path / candidate).resolve()

    def child(self, allowed_subagents: list[str] | None) -> HarnessDeps:
        """Deps for a sub-agent: same session and workdir, one level deeper, no temporary grants."""
        return HarnessDeps(
            root_path=self.root_path,
            workdir=self.workdir,
            session_id=self.session_id,
            depth=self.depth + 1,
            allowed_subagents=allowed_subagents,
        )
