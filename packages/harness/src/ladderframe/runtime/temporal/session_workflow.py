"""One session = one long-running entity workflow, id `session:<agent>:<session_id>`.

    update  submit(prompt)   queue a turn, return its id immediately
    update  wait(turn_id)    block until the turn is done
    query   turn(turn_id)    turn status (and the event-stream offset its events start at)
    query   owner()          the user the session belongs to
    signal  close()          finish the session

Each turn runs the main agent (with pydantic-ai `TemporalDurability`, so model requests and tool
calls are activities) and publishes its events to the workflow's `AgentEventStream`. After every
turn the history is snapshotted to object storage; continue-as-new carries only that snapshot's key
(and any turns submitted while the run was rolling over), so the workflow input never approaches
Temporal's payload limit. The stream keeps the events of the last few turns, so a slow reader of one
turn never sees the next turn's events; older turns report their events as expired. After `idle_timeout` without
messages the workflow writes a final snapshot and completes; the next message resumes the session
from the snapshot in a fresh run with the same workflow id.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic_ai.durable_exec.temporal import AgentEventStream
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.usage import RunUsage
    from temporalio.contrib.workflow_streams import WorkflowStreamState

    from ...core.deps import HarnessDeps
    from ..executor import TurnState
    from ..registry import get_runtime
    from .activities import (
        LoadedSession,
        LoadSessionInput,
        SaveSessionInput,
        TurnMetricsInput,
        load_session,
        record_turn,
        save_session,
    )

_ACTIVITY = {
    "start_to_close_timeout": timedelta(seconds=60),
    "retry_policy": RetryPolicy(maximum_attempts=10, maximum_interval=timedelta(seconds=30)),
}
_MAX_TURNS_KEPT = 50
"""Finished turn states kept for `wait`/`turn` queries; older ones are dropped."""
_EVENT_TURNS_KEPT = 5
"""Turns whose events stay in the stream log for `events()` readers; older turns' events are dropped."""


@dataclass
class SessionInput:
    root_path: str
    session_id: str
    user: str | None = None
    idle_timeout_seconds: float = 7 * 24 * 3600
    snapshot_key: str | None = None
    turns: int = 0
    title: str | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    stream_state: WorkflowStreamState | None = None
    pending: list[SubmitRequest] = field(default_factory=list)
    """Turns submitted while the previous run was continuing as new."""
    recent_turns: list[TurnState] = field(default_factory=list)
    """The previous run's turns whose events are still in `stream_state`."""


@dataclass
class SubmitRequest:
    turn_id: str
    prompt: str
    user: str | None = None
    """Checked against the session's user, so a submit can't land in another user's session."""


def session_workflow_id(agent: str, session_id: str) -> str:
    return f"session:{agent}:{session_id}"


@workflow.defn(name="ladderframe.Session")
class SessionWorkflow:
    @workflow.init
    def __init__(self, params: SessionInput) -> None:
        self.events = AgentEventStream(prior_state=params.stream_state)
        self.queue: list[SubmitRequest] = list(params.pending)
        self.turns: dict[str, TurnState] = {t.turn_id: t for t in params.recent_turns}
        self.turns.update({r.turn_id: TurnState(turn_id=r.turn_id) for r in params.pending})
        self.rolling_over = False
        self.closing = False
        self.messages: list[ModelMessage] = []
        self.user = params.user

    @workflow.run
    async def run(self, params: SessionInput) -> str:
        runtime = get_runtime(params.root_path)
        state = params
        continue_as_new = False
        stream_state: WorkflowStreamState | None = None
        async with self.events:
            loaded: LoadedSession = await workflow.execute_activity(
                load_session, LoadSessionInput(params.root_path, params.session_id, params.snapshot_key), **_ACTIVITY
            )
            self.messages = loaded.messages
            if params.snapshot_key is None and loaded.meta is not None:
                # Resumed after the previous run closed: keep counting from the stored totals.
                state.turns, state.usage = loaded.meta.turns, loaded.meta.usage
                state.title = state.title or loaded.meta.title
            if loaded.meta is not None and loaded.meta.user is not None:
                self.user = state.user = loaded.meta.user
            while True:
                try:
                    await workflow.wait_condition(
                        lambda: bool(self.queue) or self.closing, timeout=timedelta(seconds=params.idle_timeout_seconds)
                    )
                except TimeoutError:
                    self.closing = True  # reject late submissions; the client restarts the session
                    break
                if not self.queue:
                    break  # closing
                state = await self._run_turn(runtime, state, self.queue.pop(0))
                if _continue_as_new_suggested() and not self.queue:
                    # From here, `wait` for a turn that won't run in this run returns `TurnCarriedOver`
                    # (the client retries on the next run), so it can't hold up the stream's drain.
                    self.rolling_over = True
                    continue_as_new = True
                    break

            if continue_as_new:
                stream_state = self.events.stream.get_state()
            else:
                await self._snapshot(state, status="closed")

        if continue_as_new:
            workflow.continue_as_new(
                SessionInput(
                    root_path=state.root_path,
                    session_id=state.session_id,
                    user=state.user,
                    idle_timeout_seconds=state.idle_timeout_seconds,
                    snapshot_key=state.snapshot_key,
                    turns=state.turns,
                    title=state.title,
                    usage=state.usage,
                    stream_state=stream_state,
                    # Submitted while the stream drained; the next run picks them up.
                    pending=self.queue,
                    # States of the turns whose events the stream state still holds.
                    recent_turns=[t for t in self.turns.values() if t.offset is not None],
                )
            )
        return state.snapshot_key or ""

    async def _run_turn(self, runtime: Any, state: SessionInput, request: SubmitRequest) -> SessionInput:
        turn = self.turns[request.turn_id]
        turn.status, turn.offset = "running", _stream_offset(self.events)
        self._truncate_events()
        started = workflow.now()
        deps = HarnessDeps(
            root_path=state.root_path,
            workdir=str(runtime.workdir),
            session_id=state.session_id,
            allowed_subagents=runtime.select_tools(runtime.config.tools).allowed_subagents,
        )
        try:
            result = await runtime.agent.run(
                request.prompt, message_history=self.messages, deps=deps, usage_limits=runtime.usage_limits()
            )
        except Exception as exc:  # noqa: BLE001 — a failed turn must not end the session
            turn.status, turn.error = "failed", f"{type(exc).__name__}: {exc}"
            await self._metrics(state, "failed", started, None)
            return state
        self.messages = result.all_messages()
        state.turns += 1
        state.title = state.title or request.prompt[:80]
        state.usage = state.usage + result.usage
        await self._snapshot(state, status="open")
        # Done only once the snapshot is stored, so a caller that reads the history right after
        # `wait` returns sees this turn.
        turn.status, turn.output = "done", result.output
        await self._metrics(state, "done", started, result.usage)
        self._forget_old_turns()
        return state

    async def _snapshot(self, state: SessionInput, status: str) -> None:
        state.snapshot_key = await workflow.execute_activity(
            save_session,
            SaveSessionInput(
                root_path=state.root_path,
                session_id=state.session_id,
                messages=self.messages,
                user=state.user,
                title=state.title,
                turns=state.turns,
                status=status,
                usage=state.usage,
            ),
            **_ACTIVITY,
        )

    async def _metrics(self, state: SessionInput, status: str, started: Any, usage: RunUsage | None) -> None:
        duration = (workflow.now() - started).total_seconds()
        try:
            await workflow.execute_activity(
                record_turn,
                TurnMetricsInput(state.root_path, status, duration, usage),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError:
            workflow.logger.warning("could not record turn metrics")  # metrics must never end a session

    def _truncate_events(self) -> None:
        """Keep the events of the last `_EVENT_TURNS_KEPT` turns. Truncating a turn a client is still
        reading would make the stream client restart from the oldest event left, i.e. another turn's."""
        with_events = [t for t in self.turns.values() if t.offset is not None]
        for turn in with_events[:-_EVENT_TURNS_KEPT]:
            turn.offset, turn.events_expired = None, True
        kept = with_events[-_EVENT_TURNS_KEPT:]
        if kept:
            self.events.stream.truncate(min(t.offset for t in kept if t.offset is not None))

    def _forget_old_turns(self) -> None:
        finished = [tid for tid, t in self.turns.items() if t.status in ("done", "failed")]
        for turn_id in finished[:-_MAX_TURNS_KEPT]:
            del self.turns[turn_id]

    @workflow.update
    async def submit(self, request: SubmitRequest) -> TurnState:
        await asyncio.sleep(0)  # let the stream's publish signal settle first (see WorkflowStream docs)
        if request.turn_id not in self.turns:
            self.turns[request.turn_id] = TurnState(turn_id=request.turn_id)
            self.queue.append(request)
        return self.turns[request.turn_id]

    @submit.validator
    def _validate_submit(self, request: SubmitRequest) -> None:
        if self.closing:
            raise ApplicationError("session is closing", type="SessionClosing")
        if self.user is not None and request.user != self.user:
            raise ApplicationError("session belongs to another user", type="NotOwner")
        if not request.prompt.strip():
            raise ApplicationError("empty prompt", type="EmptyPrompt")

    @workflow.update
    async def wait(self, turn_id: str) -> TurnState:
        if turn_id not in self.turns:
            raise ApplicationError(f"unknown turn {turn_id!r}", type="UnknownTurn")
        await workflow.wait_condition(lambda: self.turns[turn_id].status in ("done", "failed") or self.rolling_over)
        if self.turns[turn_id].status not in ("done", "failed"):
            raise ApplicationError("the turn runs in the session's next run", type="TurnCarriedOver")
        return self.turns[turn_id]

    @workflow.query
    def turn(self, turn_id: str) -> TurnState | None:
        return self.turns.get(turn_id)

    @workflow.query
    def owner(self) -> str | None:
        return self.user

    @workflow.signal
    def close(self) -> None:
        self.closing = True


def _continue_as_new_suggested() -> bool:
    return workflow.info().is_continue_as_new_suggested()


def _stream_offset(events: AgentEventStream) -> int:
    # The global offset the next published event will get. WorkflowStream exposes it through its
    # offset query handler; read the same value directly since we are inside the workflow.
    return events.stream._on_offset()  # pyright: ignore[reportPrivateUsage]
