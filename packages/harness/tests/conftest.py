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


def streaming(respond):  # noqa: ANN001, ANN201
    """Wrap a FunctionModel `respond` function so it also works for streamed requests."""
    from pydantic_ai.messages import TextPart, ToolCallPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    async def stream(messages, info):  # noqa: ANN001, ANN202
        for part in respond(messages, info).parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}

    return FunctionModel(respond, stream_function=stream)
