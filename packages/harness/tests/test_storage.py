"""Object storage against a real S3 API (moto's local server) and the local backends."""

from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto.server import ThreadedMotoServer
from pydantic_ai.messages import ModelRequest, UserPromptPart
from temporalio.api.common.v1 import Payload
from temporalio.converter import StorageDriverStoreContext

from ladderframe.config.schema import StorageConfig
from ladderframe.storage import FileObjectStore, MemoryObjectStore, S3ObjectStore, SessionArchive, SessionMeta
from ladderframe.storage.object_store import ObjectStore, payload_storage_driver


@pytest.fixture(scope="module")
def s3_config() -> Iterator[StorageConfig]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    config = StorageConfig(endpoint=f"http://{host}:{port}", bucket="lf-test", access_key="a", secret_key="b")
    boto3.client(
        "s3", endpoint_url=config.endpoint, aws_access_key_id="a", aws_secret_access_key="b", region_name="us-east-1"
    ).create_bucket(Bucket="lf-test")
    yield config
    server.stop()


@pytest.fixture(params=["memory", "file", "s3"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    if request.param == "memory":
        return MemoryObjectStore()
    if request.param == "file":
        return FileObjectStore(tmp_path / "objects")
    return S3ObjectStore(request.getfixturevalue("s3_config"))


async def test_object_store_contract(store: ObjectStore) -> None:
    assert await store.get("a/missing") is None
    assert not await store.exists("a/missing")
    await store.put("a/one", b"1")
    await store.put("a/two", b"2")
    await store.put("b/three", b"3")
    assert await store.get("a/one") == b"1"
    assert await store.exists("a/two")
    assert await store.list("a/") == ["a/one", "a/two"]
    await store.delete("a/one")
    assert await store.list("a/") == ["a/two"]
    await store.ping()
    await store.close()


async def test_file_store_rejects_escaping_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await FileObjectStore(tmp_path).put("../outside", b"x")


async def test_session_archive(store: ObjectStore) -> None:
    archive = SessionArchive(store, "coder")
    messages = [ModelRequest(parts=[UserPromptPart("hello")])]
    key = await archive.save(SessionMeta(session_id="s1", agent="coder", user="ada", turns=1), messages)
    assert key == "sessions/coder/s1/history.json"
    meta, loaded = await archive.load("s1")
    assert meta is not None and meta.user == "ada"
    assert loaded[0].parts[0].content == "hello"  # type: ignore[union-attr]
    assert [m.session_id for m in await archive.list(user="ada")] == ["s1"]
    assert await archive.list(user="bob") == []
    await store.close()


async def test_payload_driver_writes_under_prefix(store: ObjectStore) -> None:
    driver = payload_storage_driver(store, "payloads/")
    payload = Payload(metadata={"encoding": b"binary/plain"}, data=b"x" * 1024)
    [claim] = await driver.store(StorageDriverStoreContext(), [payload])
    keys = await store.list("payloads/")
    assert len(keys) == 1
    [restored] = await driver.retrieve(None, [claim])  # type: ignore[arg-type]
    assert restored.data == payload.data
    await store.close()
