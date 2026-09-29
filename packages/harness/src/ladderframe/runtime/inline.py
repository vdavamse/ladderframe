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
from .executor import Executor, NativeEvent, TurnFailed, TurnState, new_id
from .runtime import Runtime


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


class InlineExecutor(Executor):
    def __init__(self, runtime: Runtime, event_stream_handler: EventStreamHandler[HarnessDeps] | None = None) -> None:
        self.runtime = runtime
        self.archive = SessionArchive(runtime.object_store, runtime.agent_name(), runtime.config.storage.session_prefix)
        self.extra_handler = event_stream_handler
        self._sessions: dict[str, _Session] = {}

    def _session(self, session_id: str) -> _Session:
        return self._sessions.setdefault(validate_session_id(session_id), _Session())

    async def submit(self, session_id: str, prompt: str, *, user: str | None = None) -> TurnState:
        session = self._session(session_id)
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

    def _turn(self, session_id: str, turn_id: str) -> _Turn:
        turn = self._session(session_id).turns.get(turn_id)
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
        meta, messages = await self.archive.load(session_id)
        if meta is not None:
            meta.status = "closed"
            await self.archive.save(meta, messages)
        self._sessions.pop(session_id, None)
