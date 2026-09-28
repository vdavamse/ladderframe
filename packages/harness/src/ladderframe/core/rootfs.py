"""Paths inside an agent root.

An agent root looks like:

    AGENTS.md  mcp.yaml  agents/  skills/  tools/  etc/  var/  tmp/

`share/` in the monorepo has the same layout and is searched after the root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_ENV = "LADDERFRAME_ROOT"
SHARE_ENV = "LADDERFRAME_SHARE"


@dataclass(frozen=True)
class Layer:
    """One directory with the agent-root layout (the root itself, `share/`, or a `.claude/` dir)."""

    path: Path

    @property
    def agents_md(self) -> Path:
        return self.path / "AGENTS.md"

    @property
    def mcp_yaml(self) -> Path:
        return self.path / "mcp.yaml"

    @property
    def agents_dir(self) -> Path:
        return self.path / "agents"

    @property
    def skills_dir(self) -> Path:
        return self.path / "skills"

    @property
    def tools_dir(self) -> Path:
        return self.path / "tools"

    @property
    def etc(self) -> Path:
        return self.path / "etc"

    @property
    def init_d(self) -> Path:
        return self.etc / "init.d"

    @property
    def cron_d(self) -> Path:
        return self.etc / "cron.d"


@dataclass(frozen=True)
class AgentRoot(Layer):
    @property
    def name(self) -> str:
        return self.path.name

    @property
    def var(self) -> Path:
        return self.path / "var"

    @property
    def var_lib(self) -> Path:
        return self.var / "lib"

    @property
    def var_log(self) -> Path:
        return self.var / "log"

    @property
    def var_run(self) -> Path:
        return self.var / "run"

    @property
    def tmp(self) -> Path:
        return self.path / "tmp"

    def ensure_runtime_dirs(self) -> None:
        for directory in (self.var_lib, self.var_log, self.var_run, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resolve(cls, root: str | Path | None = None) -> AgentRoot:
        """`--root` > `$LADDERFRAME_ROOT` > current directory."""
        candidate = Path(root or os.environ.get(ROOT_ENV) or ".").expanduser().resolve()
        if not candidate.is_dir():
            raise FileNotFoundError(f"agent root {candidate} does not exist")
        if not (candidate / "AGENTS.md").exists() and not (candidate / "etc" / "ladderframe.yaml").exists():
            raise FileNotFoundError(
                f"{candidate} is not an agent root (expected AGENTS.md or etc/ladderframe.yaml); pass --root"
            )
        os.environ["AGENT_ROOT"] = str(candidate)
        return cls(candidate)

    def find_share(self, setting: bool | str) -> Layer | None:
        """Locate the shared layer: explicit path, `$LADDERFRAME_SHARE`, or the nearest `share/` above the root."""
        if setting is False:
            return None
        if isinstance(setting, str) and setting:
            path = Path(setting).expanduser()
            path = path if path.is_absolute() else (self.path / path)
            return Layer(path.resolve()) if path.is_dir() else None
        if env := os.environ.get(SHARE_ENV):
            return Layer(Path(env).expanduser().resolve())
        for parent in self.path.parents:
            if (parent / "share").is_dir() and parent / "share" != self.path:
                return Layer(parent / "share")
        return None


def claude_layers(root: AgentRoot) -> list[Layer]:
    """`.claude/` in the root, then `~/.claude/` — only used when `claude_compat` is on."""
    layers = [Layer(root.path / ".claude"), Layer(Path.home() / ".claude")]
    return [layer for layer in layers if layer.path.is_dir()]
