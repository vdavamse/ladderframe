"""End-to-end tests against a real local Temporal dev server (downloaded on first use)."""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    PartDeltaEvent,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.run import AgentRunResultEvent
from temporalio.testing import WorkflowEnvironment

from ladderframe import Runtime
from ladderframe.runtime.temporal.client import task_queue
from ladderframe.runtime.temporal.executor import TemporalExecutor
from ladderframe.runtime.temporal.worker import build_worker
from ladderframe.storage import MemoryObjectStore

pytestmark = pytest.mark.asyncio(loop_scope="module")

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rootfs"


def model() -> FunctionModel:
    """Scripted by the prompt: `echo:<text>` calls Echo, `delegate:<text>` calls the helper sub-agent
    (which calls Echo), anything else is answered with the number of messages seen so far."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        results = [p for p in last.parts if isinstance(p, ToolReturnPart | RetryPromptPart)]
        if results:
            return ModelResponse(parts=[TextPart(f"result: {results[-1].content}")])
        prompt = str(last.parts[-1].content)
        if prompt.startswith("echo:"):
            return ModelResponse(parts=[ToolCallPart("Echo", {"text": prompt[5:]})])
        if prompt.startswith("delegate:"):
            args = {"description": "echo", "prompt": "echo:" + prompt[9:], "subagent_type": "helper"}
            return ModelResponse(parts=[ToolCallPart("Agent", args)])
        return ModelResponse(parts=[TextPart(f"seen {len(messages)} messages")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        response = respond(messages, info)
        part = response.parts[0]
        if isinstance(part, TextPart):
            words = part.content.split(" ")
            for index, word in enumerate(words):
                yield word if index == 0 else " " + word
        else:
            yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}

    return FunctionModel(respond, stream_function=stream)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[tuple[Runtime, TemporalExecutor]]:
    root = tmp_path_factory.mktemp("durable") / "agent"
    shutil.copytree(FIXTURE_ROOT, root)
    runtime = Runtime.load(root, durable=True, model=model())
    runtime.object_store = MemoryObjectStore()
    runtime.config.storage.payload_threshold_bytes = 2048  # offload most payloads, to exercise the driver
    async with await WorkflowEnvironment.start_local(namespace="default") as temporal:
        # Reuse the environment's client config, adding External Storage like `connect()` does.
        from ladderframe.runtime.temporal import client as client_module

        runtime.config.runtime.temporal.address = temporal.client.service_client.config.target_host
        client = await client_module.connect(runtime)
        executor = TemporalExecutor(runtime, client)
        async with build_worker(client, runtime):
            yield runtime, executor


async def test_turns_share_history_and_are_archived(env: tuple[Runtime, TemporalExecutor]) -> None:
    runtime, executor = env
    first = await executor.send("s1", "hello", user="ada")
    assert first.status == "done", first.error
    assert first.output == "seen 1 messages"
    second = await executor.send("s1", "again")
    assert second.output == "seen 3 messages"  # request, response, request
    history = await executor.history("s1")
    assert len(history) == 4
    [meta] = [m for m in await executor.sessions(user="ada") if m.session_id == "s1"]
    assert meta.turns == 2 and meta.status == "open"


async def test_tool_call_runs_as_activity(env: tuple[Runtime, TemporalExecutor]) -> None:
    _, executor = env
    turn = await executor.send("s2", "echo:durable")
    assert turn.output == "result: echo: durable"


async def test_events_stream_ends_with_result(env: tuple[Runtime, TemporalExecutor]) -> None:
    _, executor = env
    turn = await executor.submit("s3", "stream me please")
    events = [event async for event in executor.events("s3", turn.turn_id)]
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "seen 1 messages"
    assert any(isinstance(e, PartDeltaEvent) for e in events)
    # The next turn's stream starts at its own events, not the previous turn's.
    turn2 = await executor.submit("s3", "second")
    events2 = [event async for event in executor.events("s3", turn2.turn_id)]
    assert events2[-1].result.output == "seen 3 messages"  # type: ignore[union-attr]


async def test_subagent_runs_as_child_workflow(env: tuple[Runtime, TemporalExecutor]) -> None:
    runtime, executor = env
    turn = await executor.send("s4", "delegate:from child")
    assert turn.output == "result: result: echo: from child"
    children = [
        wf.id
        async for wf in executor.client.list_workflows("WorkflowType = 'ladderframe.Subagent'")
        if wf.id.startswith(f"session:{runtime.agent_name()}:s4:agent:helper:")
    ]
    assert len(children) == 1


async def test_closed_session_resumes_from_snapshot(env: tuple[Runtime, TemporalExecutor]) -> None:
    _, executor = env
    await executor.send("s5", "one")
    await executor.close_session("s5")
    await executor._handle("s5").result()  # the run completes after the final snapshot
    [meta] = [m for m in await executor.sessions() if m.session_id == "s5"]
    assert meta.status == "closed"
    resumed = await executor.send("s5", "two")
    assert resumed.output == "seen 3 messages"  # history came back from object storage


async def test_task_queue_name_and_offloaded_payloads(env: tuple[Runtime, TemporalExecutor]) -> None:
    runtime, _ = env
    assert task_queue(runtime) == "ladderframe-fixture"
    # Histories above the (lowered) threshold went to object storage instead of workflow history.
    assert await runtime.object_store.list("payloads/")


async def test_http_api_over_temporal(env: tuple[Runtime, TemporalExecutor]) -> None:
    import json

    import httpx

    from ladderframe.server.app import create_app

    runtime, executor = env
    runtime.config.server.protocols = ["vercel-ai"]
    app = create_app(runtime, executor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get("/ready")).json() == {"ready": True, "object_store": "ok", "temporal": "ok"}
        turn = (await http.post("/v1/sessions/h1/messages", json={"prompt": "echo:via http"})).json()
        assert turn["output"] == "result: echo: via http"

        body = (await http.post("/v1/sessions/h1/messages", json={"prompt": "streamed", "mode": "stream"})).text
        result = [f for f in body.split("\n\n") if f.startswith("event: result")]
        assert json.loads(result[0].split("data: ", 1)[1]) == {"output": "seen 5 messages"}

        chat = {
            "trigger": "submit-message",
            "id": "chat-t",
            "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "durable chat"}]}],
        }
        response = await http.post("/chat", json=chat)
        text = "".join(
            json.loads(line[6:]).get("delta", "") for line in response.text.splitlines() if line.startswith("data: {")
        )
        assert text == "seen 1 messages"
