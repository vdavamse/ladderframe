"""Prometheus metrics. No-ops when `prometheus-client` is not installed or metrics are disabled."""

from __future__ import annotations

import time
from typing import Any

from pydantic_ai.usage import RunUsage

_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1800)

try:
    import prometheus_client as prom
except ImportError:  # pragma: no cover - optional dependency
    prom = None

_AVAILABLE = prom is not None
if prom is not None:
    TURNS = prom.Counter("ladderframe_turns", "Finished agent turns", ["agent", "status"])
    TURN_SECONDS = prom.Histogram("ladderframe_turn_duration_seconds", "Turn duration", ["agent"], buckets=_BUCKETS)
    TOKENS = prom.Counter("ladderframe_tokens", "Model tokens", ["agent", "kind"])
    MODEL_REQUESTS = prom.Counter("ladderframe_model_requests", "Model requests", ["agent"])
    TOOL_CALLS = prom.Counter("ladderframe_tool_calls", "Tool calls", ["agent"])
    HTTP_REQUESTS = prom.Counter("ladderframe_http_requests", "HTTP requests", ["method", "route", "status"])
    HTTP_SECONDS = prom.Histogram(
        "ladderframe_http_request_duration_seconds", "HTTP request duration", ["method", "route"]
    )

_enabled = _AVAILABLE


def set_enabled(enabled: bool) -> None:
    global _enabled
    _enabled = enabled and _AVAILABLE


def enabled() -> bool:
    return _enabled


def record_turn(agent: str, status: str, duration_seconds: float, usage: RunUsage | None = None) -> None:
    if not _enabled:
        return
    TURNS.labels(agent, status).inc()
    TURN_SECONDS.labels(agent).observe(max(duration_seconds, 0.0))
    if usage is not None:
        TOKENS.labels(agent, "input").inc(usage.input_tokens or 0)
        TOKENS.labels(agent, "output").inc(usage.output_tokens or 0)
        MODEL_REQUESTS.labels(agent).inc(usage.requests or 0)
        TOOL_CALLS.labels(agent).inc(usage.tool_calls or 0)


def exposition() -> tuple[bytes, str]:
    if prom is None:
        return b"", "text/plain"
    return prom.generate_latest(prom.REGISTRY), prom.CONTENT_TYPE_LATEST


def start_metrics_server(port: int) -> None:
    if prom is not None:
        prom.start_http_server(port)


class HTTPMetricsMiddleware:
    """ASGI middleware counting requests by route template (not raw path, to keep label cardinality low)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not _enabled:
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            HTTP_REQUESTS.labels(scope["method"], route, str(status["code"])).inc()
            HTTP_SECONDS.labels(scope["method"], route).observe(time.perf_counter() - start)
