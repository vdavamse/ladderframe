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
