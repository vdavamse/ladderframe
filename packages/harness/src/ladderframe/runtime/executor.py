"""The executor interface the CLI and HTTP server talk to, with two implementations:

- `InlineExecutor` (runtime/inline.py): runs turns in-process; development and tests.
- `TemporalExecutor` (runtime/temporal/executor.py): each session is a Temporal entity workflow.

A session is a sequence of turns. `submit` queues a turn and returns immediately; `wait` blocks for
its result; `events` streams its pydantic-ai events, ending with an `AgentRunResultEvent`.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel
from pydantic_ai.messages import AgentStreamEvent, ModelMessage
from pydantic_ai.run import AgentRunResultEvent

from ..storage.sessions import SessionMeta

TurnStatus = Literal["queued", "running", "done", "failed"]
NativeEvent = AgentStreamEvent | AgentRunResultEvent[str]


class TurnState(BaseModel):
    turn_id: str
    status: TurnStatus = "queued"
    output: str | None = None
    error: str | None = None
    offset: int | None = None
    """Event-stream offset where the turn's events start (Temporal executor)."""
    events_expired: bool = False
    """The turn's events were dropped from the stream to make room for later turns (Temporal executor)."""


class TurnFailed(RuntimeError):
    pass


class NotOwner(PermissionError):
    """The session belongs to another user."""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Executor(ABC):
    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    @abstractmethod
    async def submit(self, session_id: str, prompt: str, *, user: str | None = None) -> TurnState:
        """Queue a turn (creating or resuming the session) and return without waiting. Raises `NotOwner`
        if the session belongs to another user."""

    @abstractmethod
    async def wait(self, session_id: str, turn_id: str) -> TurnState:
        """Wait for a turn to finish."""

    @abstractmethod
    def events(self, session_id: str, turn_id: str) -> AsyncIterator[NativeEvent]:
        """The turn's events, ending with its `AgentRunResultEvent`. Raises `TurnFailed` if it fails."""

    @abstractmethod
    async def turn(self, session_id: str, turn_id: str) -> TurnState: ...

    @abstractmethod
    async def history(self, session_id: str) -> list[ModelMessage]: ...

    @abstractmethod
    async def sessions(self, user: str | None = None) -> list[SessionMeta]: ...

    @abstractmethod
    async def close_session(self, session_id: str) -> None: ...

    @abstractmethod
    async def owner(self, session_id: str) -> str | None:
        """The user the session belongs to (`None` for an unknown or anonymous session), from its stored
        meta or, before the first snapshot, from the live session."""

    async def send(self, session_id: str, prompt: str, *, user: str | None = None) -> TurnState:
        """Queue a turn and wait for it."""
        turn = await self.submit(session_id, prompt, user=user)
        return await self.wait(session_id, turn.turn_id)
