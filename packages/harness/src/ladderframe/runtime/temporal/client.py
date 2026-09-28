"""Temporal client for a runtime: pydantic-ai's payload converter plus External Storage in MinIO/S3."""

from __future__ import annotations

import dataclasses

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.converter import DataConverter, ExternalStorage

from ...storage.object_store import payload_storage_driver
from ..runtime import Runtime


async def connect(runtime: Runtime) -> Client:
    temporal = runtime.config.runtime.temporal
    storage = runtime.config.storage
    data_converter = dataclasses.replace(
        DataConverter.default,
        external_storage=ExternalStorage(
            drivers=[payload_storage_driver(runtime.object_store, storage.payload_prefix)],
            payload_size_threshold=storage.payload_threshold_bytes,
        ),
    )
    return await Client.connect(
        temporal.address,
        namespace=temporal.namespace,
        data_converter=data_converter,
        plugins=[PydanticAIPlugin()],
    )


def task_queue(runtime: Runtime) -> str:
    return runtime.config.runtime.temporal.task_queue or f"ladderframe-{runtime.agent_name()}"
