"""Tracing and metrics, configured by `observability:` in etc/ladderframe.yaml.

tracing: otel      OpenTelemetry SDK + OTLP/HTTP exporter (standard OTEL_EXPORTER_OTLP_* variables);
                   pydantic-ai agent runs, model requests and tool calls become spans, and Temporal
                   workflows/activities get spans through Temporal's TracingInterceptor.
tracing: logfire   Pydantic Logfire (LOGFIRE_TOKEN): pydantic-ai, FastAPI, and Temporal via
                   pydantic-ai's LogfirePlugin.
metrics: true      Prometheus: `/metrics` on the server (or `metrics_port` for a worker-only process);
                   `temporal_metrics_port` also exposes the Temporal SDK's own metrics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import metrics

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime

_configured: str | None = None


def service_name(runtime: Runtime) -> str:
    return runtime.config.observability.service_name or f"ladderframe-{runtime.agent_name()}"


def configure(runtime: Runtime) -> None:
    """Set up tracing and metrics for this process. Call once, before agents run."""
    global _configured
    config = runtime.config.observability
    metrics.set_enabled(config.metrics)
    if _configured == config.tracing:
        return
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    if config.tracing == "otel":
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name(runtime)}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        Agent.instrument_all(InstrumentationSettings(include_content=config.include_content))
    elif config.tracing == "logfire":
        import logfire

        logfire.configure(service_name=service_name(runtime), send_to_logfire="if-token-present")
        logfire.instrument_pydantic_ai(include_content=config.include_content)
    _configured = config.tracing


def instrument_app(runtime: Runtime, app: Any) -> None:
    if runtime.config.observability.tracing == "logfire":
        import logfire

        logfire.instrument_fastapi(app)


def temporal_client_options(runtime: Runtime) -> dict[str, Any]:
    """Extra `Client.connect()` arguments: tracing plugin/interceptor and the SDK metrics runtime."""
    config = runtime.config.observability
    options: dict[str, Any] = {"plugins": [], "interceptors": []}
    if config.tracing == "otel":
        from temporalio.contrib.opentelemetry import TracingInterceptor

        options["interceptors"].append(TracingInterceptor())
    elif config.tracing == "logfire":
        from pydantic_ai.durable_exec.temporal import LogfirePlugin

        # With our own SDK metrics port, keep Temporal metrics out of Logfire so the runtime is ours.
        options["plugins"].append(LogfirePlugin(metrics=config.temporal_metrics_port is None))
    if config.temporal_metrics_port is not None:
        options["runtime"] = _temporal_runtime(config.temporal_metrics_port)
    return options


_temporal_runtimes: dict[int, Any] = {}


def _temporal_runtime(port: int) -> Any:
    from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

    if port not in _temporal_runtimes:
        _temporal_runtimes[port] = Runtime(
            telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address=f"0.0.0.0:{port}"))
        )
    return _temporal_runtimes[port]


__all__ = ["configure", "instrument_app", "metrics", "service_name", "temporal_client_options"]
