"""Object storage (MinIO / S3) for session snapshots and offloaded Temporal payloads.

One bucket, two prefixes (see `storage:` in etc/ladderframe.yaml):

    payloads/   Temporal External Storage: payloads above 256 KiB, written by the S3 storage driver.
                Expire them with a lifecycle rule, but only after the namespace retention period —
                a workflow whose payloads are gone cannot be replayed.
    sessions/   session history snapshots, kept indefinitely (see `storage/sessions.py`).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from ..config.schema import StorageConfig


class ObjectStore(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        """The object's bytes, or `None` if it does not exist."""

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def list(self, prefix: str) -> list[str]: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    async def ping(self) -> None:
        """Raise if the store is unreachable (used by `/ready`)."""
        await self.list("__ping__/")

    async def close(self) -> None:
        return None


class MemoryObjectStore(ObjectStore):
    """In-process store for tests and the inline executor."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.objects[key] = data

    async def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class FileObjectStore(ObjectStore):
    """Objects as files under a directory; the default for local development (`<root>/var/lib/objects`)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        path = (self.directory / key).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError(f"object key escapes the store: {key!r}")
        return path

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    async def list(self, prefix: str) -> list[str]:
        if not self.directory.exists():
            return []
        keys = (p.relative_to(self.directory).as_posix() for p in self.directory.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix) and not k.endswith(".tmp"))

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3ObjectStore(ObjectStore):
    """aioboto3-backed store; works with MinIO through `endpoint`."""

    def __init__(self, config: StorageConfig) -> None:
        self.config = config
        self.bucket = config.bucket
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def client(self) -> Any:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    import aioboto3
                    from aiobotocore.config import AioConfig

                    session = aioboto3.Session(
                        aws_access_key_id=self.config.access_key or None,
                        aws_secret_access_key=self.config.secret_key or None,
                        region_name=self.config.region,
                    )
                    stack = AsyncExitStack()
                    self._client = await stack.enter_async_context(
                        session.client(
                            "s3",
                            endpoint_url=self.config.endpoint or None,
                            config=AioConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 5}),
                        )
                    )
                    self._stack = stack
        return self._client

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        client = await self.client()
        await client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    async def get(self, key: str) -> bytes | None:
        client = await self.client()
        try:
            response = await client.get_object(Bucket=self.bucket, Key=key)
        except client.exceptions.NoSuchKey:
            return None
        async with response["Body"] as body:
            return await body.read()

    async def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        client = await self.client()
        try:
            await client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    async def list(self, prefix: str) -> list[str]:
        client = await self.client()
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    async def delete(self, key: str) -> None:
        client = await self.client()
        await client.delete_object(Bucket=self.bucket, Key=key)

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack, self._client = None, None


def open_object_store(config: StorageConfig, local_dir: Path) -> ObjectStore:
    """S3/MinIO when `storage.endpoint` is set, otherwise files under `local_dir`."""
    return S3ObjectStore(config) if config.endpoint else FileObjectStore(local_dir)


def payload_storage_driver(store: ObjectStore, prefix: str) -> Any:
    """Temporal `S3StorageDriver` writing through `store` under `prefix` (e.g. `payloads/`)."""
    from temporalio.contrib.aws.s3driver import S3StorageDriver, S3StorageDriverClient

    class _PrefixedClient(S3StorageDriverClient):
        async def put_object(self, *, bucket: str, key: str, data: bytes) -> None:
            await store.put(prefix + key, data)

        async def object_exists(self, *, bucket: str, key: str) -> bool:
            return await store.exists(prefix + key)

        async def get_object(self, *, bucket: str, key: str) -> bytes:
            data = await store.get(prefix + key)
            if data is None:
                raise KeyError(f"payload {prefix + key} not found in object storage")
            return data

        def describe(self) -> Mapping[str, str]:
            return {"store": type(store).__name__, "prefix": prefix}

    bucket = store.bucket if isinstance(store, S3ObjectStore) else "memory"
    return S3StorageDriver(client=_PrefixedClient(), bucket=bucket, driver_name="ladderframe.s3")
