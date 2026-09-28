"""Dependencies passed to every tool via `RunContext[HarnessDeps]`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from ..config import HarnessConfig
from .permissions import Permissions
from .rootfs import AgentRoot

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime

SettingsT = TypeVar("SettingsT", bound=BaseModel)


@dataclass
class HarnessDeps:
    runtime: Runtime
    root: AgentRoot
    config: HarnessConfig
    permissions: Permissions
    workdir: Path
    session_id: str
    depth: int = 0
    """0 for the main agent, +1 for each level of sub-agent."""
    allowed_subagents: list[str] | None = None
    """From `Agent(a, b)` in a tools list; `None` means every discovered sub-agent."""
    _settings_cache: dict[str, BaseModel] = field(default_factory=dict, repr=False)

    def settings(self, tool_name: str, model: type[SettingsT]) -> SettingsT:
        """Validated per-tool settings from `tool_settings.<tool_name>` in the config."""
        cached = self._settings_cache.get(tool_name)
        if not isinstance(cached, model):
            cached = model.model_validate(self.config.tool_settings.get(tool_name, {}))
            self._settings_cache[tool_name] = cached
        return cached

    def resolve_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        return (candidate if candidate.is_absolute() else self.workdir / candidate).resolve()
