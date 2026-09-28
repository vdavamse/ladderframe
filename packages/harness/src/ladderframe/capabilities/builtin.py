"""Capability names usable under `capabilities:` in `etc/ladderframe.yaml`.

    capabilities:
      - prompt_injection_defender                        # defaults
      - prompt_injection_defender: {block_high_risk: true}
      - my_package.caps:AuditLog                         # any pydantic-ai capability by import path

Installed packages can add names through the `ladderframe.capabilities` entry-point group.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

from pydantic_ai.capabilities import AbstractCapability

ENTRY_POINT_GROUP = "ladderframe.capabilities"


def _prompt_injection_defender(**options: Any) -> AbstractCapability[Any]:
    try:
        from pydantic_ai_harness import PromptInjectionDefender
    except ImportError as exc:
        raise ImportError("prompt_injection_defender needs `pip install 'ladderframe[harness-capabilities]'`") from exc
    return PromptInjectionDefender(**options)


BUILTIN_CAPABILITIES: dict[str, Callable[..., AbstractCapability[Any]]] = {
    "prompt_injection_defender": _prompt_injection_defender,
}


def _resolve_factory(name: str) -> Callable[..., AbstractCapability[Any]]:
    if name in BUILTIN_CAPABILITIES:
        return BUILTIN_CAPABILITIES[name]
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == name:
            return ep.load()
    if ":" in name:
        module_name, _, attr = name.partition(":")
        return getattr(importlib.import_module(module_name), attr)
    raise ValueError(f"unknown capability {name!r}")


def build_capabilities(entries: list[str | dict[str, dict[str, Any]]]) -> list[AbstractCapability[Any]]:
    capabilities = []
    for entry in entries:
        name, options = (entry, {}) if isinstance(entry, str) else next(iter(entry.items()))
        capabilities.append(_resolve_factory(name)(**(options or {})))
    return capabilities
