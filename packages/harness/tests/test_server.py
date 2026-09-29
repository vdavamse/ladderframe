import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ladderframe import Runtime
from ladderframe.runtime.inline import InlineExecutor
from ladderframe.server.app import create_app
from ladderframe.storage import MemoryObjectStore

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rootfs"


def echo_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(f"turn with {len(messages)} messages")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield f"turn with {len(messages)} messages"

    return FunctionModel(respond, stream_function=stream)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, model=echo_model())
    runtime.object_store = MemoryObjectStore()
    runtime.config.server.auth = "api-key"
    runtime.config.server.api_keys = ["secret"]
    runtime.config.server.protocols = ["vercel-ai"]
    with TestClient(create_app(runtime, InlineExecutor(runtime))) as test_client:
        yield test_client


AUTH = {"Authorization": "Bearer secret", "X-User-Id": "ada"}


def test_health_is_public_and_api_needs_key(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["ready"] is True
    assert client.get("/v1/sessions").status_code == 401
    assert client.get("/v1/sessions", headers={"X-API-Key": "wrong"}).status_code == 401


def test_session_round_trip(client: TestClient) -> None:
    session_id = client.post("/v1/sessions", headers=AUTH).json()["session_id"]
    first = client.post(f"/v1/sessions/{session_id}/messages", json={"prompt": "hi"}, headers=AUTH).json()
    assert first["status"] == "done" and first["output"] == "turn with 1 messages"
    second = client.post(f"/v1/sessions/{session_id}/messages", json={"prompt": "again"}, headers=AUTH).json()
    assert second["output"] == "turn with 3 messages"
    history = client.get(f"/v1/sessions/{session_id}/messages", headers=AUTH).json()
    assert len(history) == 4
    [meta] = client.get("/v1/sessions", headers=AUTH).json()
    assert meta["session_id"] == session_id and meta["user"] == "ada" and meta["turns"] == 2
    assert client.get("/v1/sessions", headers={**AUTH, "X-User-Id": "bob"}).json() == []
    assert client.delete(f"/v1/sessions/{session_id}", headers=AUTH).status_code == 204


def test_async_then_poll(client: TestClient) -> None:
    response = client.post("/v1/sessions/s-async/messages", json={"prompt": "hi", "mode": "async"}, headers=AUTH)
    assert response.status_code == 202
    turn_id = response.json()["turn_id"]
    events = client.get(f"/v1/sessions/s-async/turns/{turn_id}/events", headers=AUTH).text
    assert "event: result" in events
    state = client.get(f"/v1/sessions/s-async/turns/{turn_id}", headers=AUTH).json()
    assert state["status"] == "done"


def test_stream_mode(client: TestClient) -> None:
    body = client.post("/v1/sessions/s-stream/messages", json={"prompt": "hi", "mode": "stream"}, headers=AUTH).text
    frames = [f for f in body.split("\n\n") if f]
    assert frames[0].startswith("event: turn")
    result = frames[-1].split("data: ", 1)[1]
    assert json.loads(result) == {"output": "turn with 1 messages"}


def test_invalid_session_id(client: TestClient) -> None:
    response = client.post("/v1/sessions/bad id!/messages", json={"prompt": "hi"}, headers=AUTH)
    assert response.status_code == 422


def test_vercel_ai_chat(client: TestClient) -> None:
    payload = {
        "trigger": "submit-message",
        "id": "chat-1",
        "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}],
    }
    response = client.post("/chat", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert "turn with 1 messages" in "".join(
        json.loads(line[6:]).get("delta", "") for line in response.text.splitlines() if line.startswith("data: {")
    )
    history = client.get("/v1/sessions/chat-1/messages", headers=AUTH).json()
    assert len(history) == 2


EVE = {**AUTH, "X-User-Id": "eve"}


def test_other_users_cannot_touch_a_session(client: TestClient) -> None:
    turn = client.post("/v1/sessions/s-ada/messages", json={"prompt": "secret"}, headers=AUTH).json()
    assert client.get("/v1/sessions/s-ada/messages", headers=EVE).status_code == 404
    assert client.get(f"/v1/sessions/s-ada/turns/{turn['turn_id']}", headers=EVE).status_code == 404
    assert client.get(f"/v1/sessions/s-ada/turns/{turn['turn_id']}/events", headers=EVE).status_code == 404
    assert client.post("/v1/sessions/s-ada/messages", json={"prompt": "hi"}, headers=EVE).status_code == 404
    assert client.delete("/v1/sessions/s-ada", headers=EVE).status_code == 404
    chat = {
        "trigger": "submit-message",
        "id": "s-ada",
        "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}],
    }
    assert client.post("/chat", json=chat, headers=EVE).status_code == 404
    # ada's session is untouched
    assert len(client.get("/v1/sessions/s-ada/messages", headers=AUTH).json()) == 2
    [meta] = client.get("/v1/sessions", headers=AUTH).json()
    assert meta["status"] == "open" and meta["turns"] == 1


async def test_session_is_owned_before_its_first_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, model=echo_model())
    runtime.object_store = MemoryObjectStore()
    executor = InlineExecutor(runtime)
    turn = await executor.submit("s-new", "hi", user="ada")
    assert await executor.owner("s-new") == "ada"  # before the turn has saved the meta
    await executor.wait("s-new", turn.turn_id)
    assert await executor.owner("s-new") == "ada"
    assert await executor.owner("s-unknown") is None


def test_web_protocol_needs_auth_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, model=echo_model())
    runtime.config.server.auth = "api-key"
    runtime.config.server.api_keys = ["secret"]
    runtime.config.server.protocols = ["web"]
    with pytest.raises(ValueError, match="without authentication"):
        create_app(runtime, InlineExecutor(runtime))


def test_ready_hides_error_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, model=echo_model())
    store = MemoryObjectStore()

    async def down() -> None:
        raise ConnectionError("https://internal-minio:9000/bucket?X-Amz-Signature=abc")

    store.ping = down  # type: ignore[method-assign]
    runtime.object_store = store
    runtime.config.server.protocols = ["vercel-ai"]
    with TestClient(create_app(runtime, InlineExecutor(runtime))) as test_client:
        response = test_client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"ready": False, "object_store": "error"}


async def test_close_during_a_turn_keeps_turns_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    release = asyncio.Event()

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            await release.wait()  # the first turn is still running when the session is closed
        return ModelResponse(parts=[TextPart(f"turn with {len(messages)} messages")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield str((await respond(messages, info)).parts[0].content)  # type: ignore[union-attr]

    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, model=FunctionModel(respond, stream_function=stream))
    runtime.object_store = MemoryObjectStore()
    executor = InlineExecutor(runtime)
    first = await executor.submit("s-close", "one", user="ada")
    await asyncio.sleep(0.05)
    close = asyncio.create_task(executor.close_session("s-close"))
    second = await executor.submit("s-close", "two", user="ada")
    release.set()
    await close
    assert (await executor.wait("s-close", first.turn_id)).output == "turn with 1 messages"
    assert (await executor.wait("s-close", second.turn_id)).output == "turn with 3 messages"  # saw turn one
    assert len(await executor.history("s-close")) == 4


async def test_submit_to_another_users_session_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from ladderframe.runtime.executor import NotOwner

    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, model=echo_model())
    runtime.object_store = MemoryObjectStore()
    executor = InlineExecutor(runtime)
    turn = await executor.submit("s-race", "mine", user="ada")
    with pytest.raises(NotOwner):  # even before ada's turn has saved the meta
        await executor.submit("s-race", "theirs", user="eve")
    assert (await executor.wait("s-race", turn.turn_id)).status == "done"
