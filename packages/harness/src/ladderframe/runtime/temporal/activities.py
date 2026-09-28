"""Activities for session snapshots in object storage (MinIO / S3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage
from temporalio import activity

from ...storage.sessions import SessionArchive, SessionMeta
from ..registry import get_runtime


@dataclass
class LoadSessionInput:
    root_path: str
    session_id: str
    snapshot_key: str | None = None
    """History key carried across continue-as-new; `None` loads the session's latest snapshot."""


@dataclass
class SaveSessionInput:
    root_path: str
    session_id: str
    messages: list[ModelMessage]
    user: str | None = None
    title: str | None = None
    turns: int = 0
    status: str = "open"
    usage: RunUsage = field(default_factory=RunUsage)


def _archive(root_path: str) -> SessionArchive:
    runtime = get_runtime(root_path)
    return SessionArchive(runtime.object_store, runtime.agent_name(), runtime.config.storage.session_prefix)


@activity.defn(name="ladderframe.load_session")
async def load_session(params: LoadSessionInput) -> list[ModelMessage]:
    archive = _archive(params.root_path)
    if params.snapshot_key:
        return await archive.load_history(params.snapshot_key)
    return (await archive.load(params.session_id))[1]


@activity.defn(name="ladderframe.save_session")
async def save_session(params: SaveSessionInput) -> str:
    """Write the snapshot; returns the history key (what continue-as-new carries instead of the history)."""
    archive = _archive(params.root_path)
    existing, _ = await archive.load(params.session_id)
    meta = existing or SessionMeta(session_id=params.session_id, agent=archive.agent, user=params.user)
    meta.title = meta.title or params.title
    meta.turns = params.turns
    meta.messages = len(params.messages)
    meta.status = params.status
    meta.usage = params.usage
    meta.updated_at = datetime.now(UTC)
    return await archive.save(meta, params.messages)


ACTIVITIES = [load_session, save_session]
