"""Find sub-agents, skills and custom tools at startup.

Search order (first match by name wins): the agent root, then `share/`, then — with
`claude_compat: true` — `<root>/.claude/` and `~/.claude/`.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ..tools.base import get_meta
from .frontmatter import SkillSpec, SubagentSpec
from .rootfs import Layer

log = logging.getLogger(__name__)

TOOL_ENTRY_POINTS = "ladderframe.tools"


@dataclass
class Discovered:
    subagents: dict[str, SubagentSpec] = field(default_factory=dict)
    skills: dict[str, SkillSpec] = field(default_factory=dict)
    tools: dict[str, Callable[..., Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def discover(layers: list[Layer]) -> Discovered:
    found = Discovered()
    for layer in layers:
        _discover_subagents(layer, found)
        _discover_skills(layer, found)
        _discover_tools(layer, found)
    return found


def _discover_subagents(layer: Layer, found: Discovered) -> None:
    if not layer.agents_dir.is_dir():
        return
    for path in sorted(layer.agents_dir.glob("*.md")):
        try:
            spec = SubagentSpec.from_file(path)
        except Exception as exc:  # noqa: BLE001 — report every broken file, keep loading the rest
            found.errors.append(f"{path}: {exc}")
            continue
        found.subagents.setdefault(spec.name, spec)


def _discover_skills(layer: Layer, found: Discovered) -> None:
    if not layer.skills_dir.is_dir():
        return
    for skill_md in sorted(layer.skills_dir.glob("*/SKILL.md")):
        try:
            spec = SkillSpec.from_dir(skill_md.parent)
        except Exception as exc:  # noqa: BLE001
            found.errors.append(f"{skill_md}: {exc}")
            continue
        found.skills.setdefault(spec.name, spec)


def _discover_tools(layer: Layer, found: Discovered) -> None:
    if not layer.tools_dir.is_dir():
        return
    for path in sorted(layer.tools_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _import_file(path)
        except Exception as exc:  # noqa: BLE001
            found.errors.append(f"{path}: failed to import: {exc}")
            continue
        for fn in tools_in(module):
            found.tools.setdefault(get_meta(fn).name, fn)  # type: ignore[union-attr]


def tools_in(namespace: Any) -> list[Callable[..., Any]]:
    """All `@tool`-decorated callables in a module (or a single tool / list of tools)."""
    if get_meta(namespace):
        return [namespace]
    if isinstance(namespace, list | tuple):
        return [t for t in namespace if get_meta(t)]
    return [obj for _, obj in inspect.getmembers(namespace) if callable(obj) and get_meta(obj)]


def entry_point_tools(packages: list[str]) -> tuple[dict[str, Callable[..., Any]], list[str]]:
    """Tools from installed distributions listed in `tool_packages`."""
    tools: dict[str, Callable[..., Any]] = {}
    errors: list[str] = []
    wanted = {p.replace("_", "-").lower() for p in packages}
    for ep in entry_points(group=TOOL_ENTRY_POINTS):
        dist = (ep.dist.name if ep.dist else "").replace("_", "-").lower()
        if dist not in wanted:
            continue
        try:
            for fn in tools_in(ep.load()):
                tools.setdefault(get_meta(fn).name, fn)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"entry point {ep.name} ({dist}): {exc}")
    return tools, errors


def _import_file(path: Path) -> Any:
    module_name = f"ladderframe_user_tools_{path.parent.parent.name}_{path.stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
