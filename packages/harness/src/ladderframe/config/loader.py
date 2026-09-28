"""Load `etc/ladderframe.yaml`: built-in defaults < root config < profile overlay, with `${VAR}` expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .schema import HarnessConfig

DEFAULTS_FILE = Path(__file__).with_name("defaults.yaml")

# Same syntax as pydantic-ai's MCP config loader: ${VAR} and ${VAR:-default}.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def expand_env_vars(value: Any, missing: set[str] | None = None) -> Any:
    """Recursively expand `${VAR}` / `${VAR:-default}`.

    Undefined variables without a default expand to an empty string and are collected in `missing`,
    so a dev setup without production secrets still loads; `ladderframe check` reports them.
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, has_default, default = match.group(1), match.group(2) is not None, match.group(3)
            if name in os.environ:
                return os.environ[name]
            if has_default:
                return default or ""
            if missing is not None:
                missing.add(name)
            return ""

        return _ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(v, missing) for v in value]
    return value


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Mappings merge recursively; lists and scalars from the overlay replace the base value."""
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def load_config(etc_dir: Path, profile: str | None = None, missing: set[str] | None = None) -> HarnessConfig:
    data = read_yaml(DEFAULTS_FILE)
    base = etc_dir / "ladderframe.yaml"
    if base.exists():
        data = deep_merge(data, read_yaml(base))
    profile = profile or os.environ.get("LADDERFRAME_PROFILE")
    if profile:
        overlay = etc_dir / f"ladderframe.{profile}.yaml"
        if not overlay.exists():
            raise ConfigError(f"profile {profile!r} requested but {overlay} does not exist")
        data = deep_merge(data, read_yaml(overlay))
    try:
        return HarnessConfig.model_validate(expand_env_vars(data, missing))
    except ValueError as exc:
        raise ConfigError(f"{base}: {exc}") from exc


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str | float | int) -> float:
    """`90`, `90s`, `15m`, `2h`, `7d`, `1w` -> seconds."""
    if isinstance(value, int | float):
        return float(value)
    match = _DURATION.match(value)
    if not match:
        raise ConfigError(f"invalid duration {value!r}; use e.g. 30s, 15m, 2h, 7d")
    return float(match.group(1)) * _UNITS[match.group(2)]
