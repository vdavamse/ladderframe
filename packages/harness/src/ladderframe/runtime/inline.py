"""In-process executor. Session history goes to the same object-store layout the Temporal executor
uses (`sessions/<agent>/<id>/`), under `<root>/var/lib/objects/` unless `storage.endpoint` is set.

Used by the CLI, by `ladderframe serve` with `runtime.executor: inline`, and in tests. Runs are not
durable: a crash loses the turn in progress.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai import RunContext
from pydantic_ai.agent.abstract import EventStreamHandler
from pydantic_ai.messages import AgentStreamEvent, ModelMessage
from pydantic_ai.run import AgentRunResultEvent

from ..core.deps import HarnessDeps
from ..observability import metrics
from ..storage.sessions import SessionArchive, SessionMeta, validate_session_id
from .executor import Executor, NativeEvent, NotOwner, TurnFailed, TurnState, new_id
from .runtime import Runtime

_MAX_TURNS_KEPT = 20
"""Finished turns (with their events) kept per session for `wait`/`turn`/`events`; older ones are dropped."""
_MAX_IDLE_SESSIONS = 1000
"""In-memory sessions with nothing running that are kept; the least recently used are dropped (their
history stays in the archive)."""


@dataclass
class _Turn:
    state: TurnState
    prompt: str
    events: list[NativeEvent] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Session:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: dict[str, _Turn] = field(default_factory=dict)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    user: str | None = None
    """The user who started the session, until its first snapshot records it in the meta."""


class InlineExecutor(Executor):
    def __init__(self, runtime: Runtime, event_stream_handler: EventStreamHandler[HarnessDeps] | None = None) -> None:
        self.runtime = runtime
        self.archive = SessionArchive(runtime.object_store, runtime.agent_name(), runtime.config.storage.session_prefix)
        self.extra_handler = event_stream_handler
        self._sessions: dict[str, _Session] = {}

    def _session(self, session_id: str) -> _Session:
        """The session's in-memory state, created if needed and marked most recently used."""
        session_id = validate_session_id(session_id)
        session = self._sessions.pop(session_id, None) or _Session()
        self._sessions[session_id] = session
        self._evict_idle()
        return session

    def _evict_idle(self) -> None:
        idle = [sid for sid, s in self._sessions.items() if not s.tasks]
        for session_id in idle[: max(0, len(idle) - _MAX_IDLE_SESSIONS)]:
            del self._sessions[session_id]

    async def submit(self, session_id: str, prompt: str, *, user: str | None = None) -> TurnState:
        session = self._session(session_id)
        if session.user is None:
            session.user = user
        elif user != session.user:  # checked with no await since the lookup, so two users can't race
            raise NotOwner(session_id)
        turn = _Turn(TurnState(turn_id=new_id()), prompt)
        session.turns[turn.state.turn_id] = turn
        task = asyncio.create_task(self._run(session_id, session, turn, user))
        session.tasks.add(task)
        task.add_done_callback(session.tasks.discard)
        return turn.state.model_copy()

    async def _run(self, session_id: str, session: _Session, turn: _Turn, user: str | None) -> None:
        async with session.lock:  # turns of one session run one at a time, in order
            turn.state.status = "running"
            started = time.monotonic()
            meta, history = await self.archive.load(session_id)
            meta = meta or SessionMeta(session_id=session_id, agent=self.runtime.agent_name(), user=user)

            async def record(ctx: RunContext[HarnessDeps], stream: AsyncIterable[AgentStreamEvent]) -> None:
                async def tee() -> AsyncIterator[AgentStreamEvent]:
                    async for event in stream:
                        turn.events.append(event)
                        turn.changed.set()
                        yield event

                if self.extra_handler:
                    await self.extra_handler(ctx, tee())
                else:
                    async for _ in tee():
                        pass

            try:
                result = await self.runtime.run(
                    turn.prompt, message_history=history, session_id=session_id, event_stream_handler=record
                )
            except Exception as exc:  # noqa: BLE001 — the failure is reported on the turn
                turn.state.status, turn.state.error = "failed", f"{type(exc).__name__}: {exc}"
                metrics.record_turn(self.runtime.agent_name(), "failed", time.monotonic() - started)
            else:
                messages = result.all_messages()
                meta.turns += 1
                meta.messages = len(messages)
                meta.status = "open"
                meta.updated_at = datetime.now(UTC)
                meta.usage = meta.usage + result.usage
                meta.title = meta.title or turn.prompt[:80]
                await self.archive.save(meta, messages)
                turn.events.append(AgentRunResultEvent(result))
                turn.state.status, turn.state.output = "done", result.output
                metrics.record_turn(self.runtime.agent_name(), "done", time.monotonic() - started, result.usage)
            finally:
                turn.changed.set()
                turn.done.set()
                _forget_old_turns(session)

    def _turn(self, session_id: str, turn_id: str) -> _Turn:
        session = self._sessions.get(validate_session_id(session_id))
        turn = session.turns.get(turn_id) if session else None
        if turn is None:
            raise KeyError(f"unknown turn {turn_id!r} in session {session_id!r}")
        return turn

    async def wait(self, session_id: str, turn_id: str) -> TurnState:
        turn = self._turn(session_id, turn_id)
        await turn.done.wait()
        return turn.state.model_copy()

    async def turn(self, session_id: str, turn_id: str) -> TurnState:
        return self._turn(session_id, turn_id).state.model_copy()

    async def events(self, session_id: str, turn_id: str) -> AsyncIterator[NativeEvent]:
        turn = self._turn(session_id, turn_id)
        index = 0
        while True:
            while index < len(turn.events):
                yield turn.events[index]
                index += 1
            if turn.done.is_set():
                if turn.state.status == "failed":
                    raise TurnFailed(turn.state.error or "turn failed")
                return
            turn.changed.clear()
            if index >= len(turn.events) and not turn.done.is_set():
                await turn.changed.wait()

    async def history(self, session_id: str) -> list[ModelMessage]:
        return (await self.archive.load(session_id))[1]

    async def sessions(self, user: str | None = None) -> list[SessionMeta]:
        return await self.archive.list(user)

    async def close_session(self, session_id: str) -> None:
        session = self._session(session_id)
        async with session.lock:  # after the running turn, so it can't overwrite the closed status
            meta, messages = await self.archive.load(session_id)
            if meta is not None:
                meta.status = "closed"
                await self.archive.save(meta, messages)
        if not session.tasks and self._sessions.get(session_id) is session:
            del self._sessions[session_id]

    async def owner(self, session_id: str) -> str | None:
        meta = await self.archive.load_meta(session_id)
        if meta is not None:
            return meta.user
        session = self._sessions.get(validate_session_id(session_id))
        return session.user if session else None


def _forget_old_turns(session: _Session) -> None:
    finished = [tid for tid, t in session.turns.items() if t.done.is_set()]
    for turn_id in finished[:-_MAX_TURNS_KEPT]:
        del session.turns[turn_id]
