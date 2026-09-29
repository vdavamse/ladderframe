"""Wire runtime, executor and (for Temporal) the worker into one process."""

from __future__ import annotations

from contextlib import AsyncExitStack

from fastapi import FastAPI

from ..observability import configure as configure_observability
from ..runtime.executor import Executor
from ..runtime.inline import InlineExecutor
from ..runtime.runtime import Runtime
from .app import create_app


def build_app(runtime: Runtime, *, with_worker: bool = True) -> FastAPI:
    configure_observability(runtime)
    if runtime.config.runtime.executor == "inline":
        return create_app(runtime, InlineExecutor(runtime))

    from ..runtime.temporal.executor import TemporalExecutor
    from ..runtime.temporal.worker import build_worker

    executor: Executor = TemporalExecutor(runtime)

    async def worker() -> AsyncExitStack:
        stack = AsyncExitStack()
        if with_worker:
            await stack.enter_async_context(build_worker(executor.client, runtime))  # type: ignore[attr-defined]
        return stack

    return create_app(runtime, executor, background=worker)


async def run_worker(runtime: Runtime) -> None:
    """`ladderframe worker`: the Temporal worker alone, no HTTP server."""
    from ..runtime.temporal.client import connect
    from ..runtime.temporal.worker import build_worker

    configure_observability(runtime)
    port = runtime.config.observability.metrics_port
    if runtime.config.observability.metrics and port:
        from ..observability.metrics import start_metrics_server

        start_metrics_server(port)
    client = await connect(runtime)
    await build_worker(client, runtime).run()
