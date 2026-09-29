from pathlib import Path

import pytest
from conftest import streaming
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

from ladderframe import Runtime
from ladderframe.evals import find_datasets, run_dataset

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rootfs"


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    last = messages[-1].parts[-1]
    if isinstance(last, ToolReturnPart):
        return ModelResponse(parts=[TextPart(str(last.content))])
    if str(last.content).startswith("echo:"):
        return ModelResponse(parts=[ToolCallPart("Echo", {"text": str(last.content)[5:]})])
    return ModelResponse(parts=[TextPart("hello back")])


async def test_dataset_runs_and_counts_assertions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, durable=False, model=streaming(respond))
    [path] = find_datasets(runtime)
    result = await run_dataset(runtime, path)
    assert (result.passed, result.total) == (2, 3)
    assert result.pass_rate == pytest.approx(2 / 3)
    assert find_datasets(runtime, ["nope"]) == []
