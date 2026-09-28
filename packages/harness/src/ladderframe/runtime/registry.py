"""Process-wide registry of loaded runtimes, keyed by agent root path.

Tools receive serializable `HarnessDeps` (so they can cross into Temporal activities) and find the
live `Runtime` here. A root that was not registered is loaded on first use, with the profile from
`$LADDERFRAME_PROFILE`.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import Runtime

_runtimes: dict[str, Runtime] = {}
_lock = threading.RLock()


def _key(root: str | Path) -> str:
    return str(Path(root).resolve())


def register_runtime(runtime: Runtime) -> None:
    with _lock:
        _runtimes[_key(runtime.root.path)] = runtime


def get_runtime(root: str | Path) -> Runtime:
    key = _key(root)
    runtime = _runtimes.get(key)
    if runtime is None:
        from .runtime import Runtime

        with _lock:
            runtime = _runtimes.get(key) or Runtime.load(key)
            _runtimes[key] = runtime
    return runtime


def clear_registry() -> None:
    with _lock:
        _runtimes.clear()
