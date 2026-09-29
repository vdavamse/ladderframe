"""Session snapshots in object storage: `sessions/<agent>/<session_id>/{history.json,meta.json}`.

Written after every turn and on close, so a session can be resumed after its Temporal workflow has
closed and its history has passed the namespace retention period.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.usage import RunUsage

from .object_store import ObjectStore

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def validate_session_id(session_id: str) -> str:
    if not _SAFE_ID.match(session_id):
        raise ValueError(f"invalid session id {session_id!r}: use 1-128 of [A-Za-z0-9_.-]")
    return session_id


def count_user_turns(messages: list[ModelMessage]) -> int:
    return sum(
        1
        for message in messages
        if message.kind == "request" and any(part.part_kind == "user-prompt" for part in message.parts)
    )


class SessionMeta(BaseModel):
    session_id: str
    agent: str
    user: str | None = None
    title: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    turns: int = 0
    messages: int = 0
    status: str = "open"
    usage: RunUsage = Field(default_factory=RunUsage)


class SessionArchive:
    def __init__(self, store: ObjectStore, agent: str, prefix: str = "sessions/") -> None:
        self.store = store
        self.agent = agent
        self.prefix = prefix

    def _key(self, session_id: str, name: str) -> str:
        return f"{self.prefix}{self.agent}/{validate_session_id(session_id)}/{name}"

    def history_key(self, session_id: str) -> str:
        return self._key(session_id, "history.json")

    async def save(self, meta: SessionMeta, messages: list[ModelMessage]) -> str:
        """Write history then meta (meta last, so a listed session always has its history). Returns the history key."""
        key = self.history_key(meta.session_id)
        await self.store.put(key, ModelMessagesTypeAdapter.dump_json(messages), "application/json")
        await self.store.put(
            self._key(meta.session_id, "meta.json"), meta.model_dump_json().encode(), "application/json"
        )
        return key

    async def load(self, session_id: str) -> tuple[SessionMeta | None, list[ModelMessage]]:
        meta_raw = await self.store.get(self._key(session_id, "meta.json"))
        history_raw = await self.store.get(self.history_key(session_id))
        meta = SessionMeta.model_validate_json(meta_raw) if meta_raw else None
        messages = ModelMessagesTypeAdapter.validate_json(history_raw) if history_raw else []
        return meta, messages

    async def load_history(self, key: str) -> list[ModelMessage]:
        raw = await self.store.get(key)
        return ModelMessagesTypeAdapter.validate_json(raw) if raw else []

    async def list(self, user: str | None = None) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for key in await self.store.list(f"{self.prefix}{self.agent}/"):
            if key.endswith("/meta.json") and (raw := await self.store.get(key)):
                meta = SessionMeta.model_validate_json(raw)
                if user is None or meta.user == user:
                    metas.append(meta)
        return sorted(metas, key=lambda m: m.updated_at, reverse=True)
