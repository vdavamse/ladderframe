from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import streaming
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

import ladderframe.observability as observability
from ladderframe import Runtime
from ladderframe.runtime.inline import InlineExecutor
from ladderframe.server.app import create_app
from ladderframe.storage import MemoryObjectStore

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rootfs"


def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("ok")])


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    monkeypatch.chdir(FIXTURE_ROOT)
    runtime = Runtime.load(FIXTURE_ROOT, model=streaming(answer))
    runtime.object_store = MemoryObjectStore()
    return runtime


def test_metrics_endpoint_counts_turns_and_requests(runtime: Runtime) -> None:
    observability.configure(runtime)
    with TestClient(create_app(runtime, InlineExecutor(runtime))) as client:
        assert client.post("/v1/sessions/m1/messages", json={"prompt": "hi"}).json()["status"] == "done"
        body = client.get("/metrics").text
    assert 'ladderframe_turns_total{agent="fixture",status="done"}' in body
    assert 'ladderframe_model_requests_total{agent="fixture"}' in body
    assert 'route="/v1/sessions/{session_id}/messages"' in body  # route template, not the raw path


def test_metrics_can_be_disabled(runtime: Runtime) -> None:
    runtime.config.observability.metrics = False
    runtime.config.server.protocols = ["vercel-ai"]
    with TestClient(create_app(runtime, InlineExecutor(runtime))) as client:
        assert client.get("/metrics").status_code == 404


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", lambda *a, **k: exporter
    )
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", _SimpleProcessor)
    yield exporter
    Agent.instrument_all(False)
    observability._configured = None


def _SimpleProcessor(exporter: InMemorySpanExporter):  # noqa: N802, ANN202
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    return SimpleSpanProcessor(exporter)


async def test_otel_tracing_records_agent_spans(runtime: Runtime, spans: InMemorySpanExporter) -> None:
    runtime.config.observability.tracing = "otel"
    runtime.config.observability.include_content = False
    observability.configure(runtime)
    turn = await InlineExecutor(runtime).send("t1", "secret prompt")
    assert turn.status == "done"
    finished = spans.get_finished_spans()
    names = {span.name for span in finished}
    assert any(name.startswith("invoke_agent") or name == "agent run" for name in names), names
    assert all("secret prompt" not in str(span.attributes) for span in finished)  # include_content: false
