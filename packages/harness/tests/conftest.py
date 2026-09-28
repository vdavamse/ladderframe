from pathlib import Path
from types import SimpleNamespace

import pytest

from ladderframe import Runtime

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rootfs"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    monkeypatch.chdir(FIXTURE_ROOT)
    return Runtime.load(FIXTURE_ROOT)


@pytest.fixture
def ctx(runtime: Runtime) -> SimpleNamespace:
    """Stand-in for RunContext: built-in tools only use `ctx.deps`."""
    return SimpleNamespace(deps=runtime.new_deps("test-session"))
