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
    tasks: bool = False
    """A sub-agent (Agent tool) conversation rather than a user session."""


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
    tasks: bool = False
    agent: str | None = None
    """Recorded in the meta; defaults to the archive's agent name (a sub-agent's name for tasks)."""


def _archive(root_path: str, tasks: bool = False) -> SessionArchive:
    runtime = get_runtime(root_path)
    return runtime.task_archive() if tasks else runtime.session_archive()


@dataclass
class LoadedSession:
    messages: list[ModelMessage]
    meta: SessionMeta | None = None
    """The stored meta when the latest snapshot was loaded (not with `snapshot_key`)."""


@activity.defn(name="ladderframe.load_session")
async def load_session(params: LoadSessionInput) -> LoadedSession:
    archive = _archive(params.root_path, params.tasks)
    if params.snapshot_key:
        return LoadedSession(await archive.load_history(params.snapshot_key))
    meta, messages = await archive.load(params.session_id)
    return LoadedSession(messages, meta)


@activity.defn(name="ladderframe.save_session")
async def save_session(params: SaveSessionInput) -> str:
    """Write the snapshot; returns the history key (what continue-as-new carries instead of the history)."""
    archive = _archive(params.root_path, params.tasks)
    existing, _ = await archive.load(params.session_id)
    meta = existing or SessionMeta(session_id=params.session_id, agent=params.agent or archive.agent, user=params.user)
    meta.title = meta.title or params.title
    meta.turns = params.turns
    meta.messages = len(params.messages)
    meta.status = params.status
    meta.usage = params.usage
    meta.updated_at = datetime.now(UTC)
    return await archive.save(meta, params.messages)


@dataclass
class TurnMetricsInput:
    root_path: str
    status: str
    duration_seconds: float
    usage: RunUsage | None = None


@activity.defn(name="ladderframe.record_turn")
async def record_turn(params: TurnMetricsInput) -> None:
    """Metrics are process-local side effects, so the workflow reports them through an activity."""
    from ...observability.metrics import record_turn as record

    record(get_runtime(params.root_path).agent_name(), params.status, params.duration_seconds, params.usage)


@dataclass
class SaveToolOutputInput:
    root_path: str
    text: str


@activity.defn(name="ladderframe.save_tool_output")
async def save_tool_output(params: SaveToolOutputInput) -> str:
    """Full text of a truncated tool result, written on the worker (see tools/guard.py)."""
    from ...tools import truncate

    deps = get_runtime(params.root_path).new_deps()
    return truncate.write_output(truncate.output_dir(deps), params.text)


ACTIVITIES = [load_session, save_session, record_turn, save_tool_output]
