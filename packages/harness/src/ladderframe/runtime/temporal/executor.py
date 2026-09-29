"""Executor backed by the session entity workflow."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

from pydantic_ai.durable_exec.temporal import stream_agent_events
from pydantic_ai.messages import ModelMessage
from pydantic_ai.run import AgentRunResultEvent
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowQueryFailedError, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from ...config.loader import parse_duration
from ...storage.sessions import SessionArchive, SessionMeta, validate_session_id
from ..executor import Executor, NativeEvent, NotOwner, TurnFailed, TurnState, new_id
from ..runtime import EVENT_TOPIC, Runtime
from .client import connect, task_queue
from .session_workflow import SessionInput, SessionWorkflow, SubmitRequest, session_workflow_id

_STATUS_POLL = 1.0
_ROLLOVER_WAIT = 120.0
"""How long `wait` keeps retrying while a run continues as new (its stream drains for up to 30 s)."""
_CARRIED_OVER = {"TurnCarriedOver", "AcceptedUpdateCompletedWorkflow"}


class TemporalExecutor(Executor):
    def __init__(self, runtime: Runtime, client: Client | None = None) -> None:
        self.runtime = runtime
        self._client = client
        self.archive = SessionArchive(runtime.object_store, runtime.agent_name(), runtime.config.storage.session_prefix)

    async def start(self) -> None:
        if self._client is None:
            self._client = await connect(self.runtime)

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("TemporalExecutor.start() has not been awaited")
        return self._client

    def _workflow_id(self, session_id: str) -> str:
        return session_workflow_id(self.runtime.agent_name(), validate_session_id(session_id))

    def _handle(self, session_id: str):  # noqa: ANN202
        return self.client.get_workflow_handle_for(SessionWorkflow.run, self._workflow_id(session_id))

    async def submit(self, session_id: str, prompt: str, *, user: str | None = None) -> TurnState:
        request = SubmitRequest(turn_id=new_id(), prompt=prompt, user=user)
        temporal = self.runtime.config.runtime.temporal
        for attempt in range(3):
            start = WithStartWorkflowOperation(
                SessionWorkflow.run,
                SessionInput(
                    root_path=str(self.runtime.root.path),
                    session_id=session_id,
                    user=user,
                    idle_timeout_seconds=parse_duration(temporal.session_idle_timeout),
                ),
                id=self._workflow_id(session_id),
                task_queue=task_queue(self.runtime),
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            try:
                return await self.client.execute_update_with_start_workflow(
                    SessionWorkflow.submit, request, start_workflow_operation=start
                )
            except WorkflowUpdateFailedError as exc:
                if _cause_type(exc) == "NotOwner":
                    raise NotOwner(session_id) from exc
                if not _is_closing(exc) or attempt == 2:
                    raise
                # The session went idle and is finishing; wait for it, then start a fresh run.
                await self._handle(session_id).result()
        raise AssertionError("unreachable")

    async def wait(self, session_id: str, turn_id: str) -> TurnState:
        deadline = asyncio.get_running_loop().time() + _ROLLOVER_WAIT
        while True:
            try:
                return await self._handle(session_id).execute_update(SessionWorkflow.wait, turn_id)
            except WorkflowUpdateFailedError as exc:
                # The run is continuing as new before the turn ran; the next run carries it over.
                if _cause_type(exc) not in _CARRIED_OVER or asyncio.get_running_loop().time() > deadline:
                    raise
                await asyncio.sleep(_STATUS_POLL / 4)

    async def turn(self, session_id: str, turn_id: str) -> TurnState:
        state = await self._handle(session_id).query(SessionWorkflow.turn, turn_id)
        if state is None:
            raise KeyError(f"unknown turn {turn_id!r} in session {session_id!r}")
        return state

    async def events(self, session_id: str, turn_id: str) -> AsyncIterator[NativeEvent]:
        handle = self._handle(session_id)
        state = await self.turn(session_id, turn_id)
        while state.status == "queued":
            await asyncio.sleep(_STATUS_POLL / 4)
            state = await self.turn(session_id, turn_id)
        if state.events_expired:
            raise TurnFailed("the turn's events have expired; read its result with wait()")
        if state.status == "failed" or state.offset is None:
            raise TurnFailed(state.error or "turn failed")

        stream = stream_agent_events(
            self.client,
            handle,
            EVENT_TOPIC,
            output_type=str,
            from_offset=state.offset,
            poll_cooldown=timedelta(milliseconds=100),
        )
        iterator = aiter(stream)
        next_event: asyncio.Task[NativeEvent] | None = None
        try:
            while True:
                next_event = next_event or asyncio.ensure_future(anext(iterator))
                done, _ = await asyncio.wait({next_event}, timeout=_STATUS_POLL)
                if not done:
                    # A failed run publishes no terminal event; notice the failure through the turn state.
                    state = await self.turn(session_id, turn_id)
                    if state.status == "failed":
                        raise TurnFailed(state.error or "turn failed")
                    continue
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                next_event = None
                yield event
                if isinstance(event, AgentRunResultEvent):
                    return
        finally:
            if next_event is not None and not next_event.done():
                next_event.cancel()

    async def history(self, session_id: str) -> list[ModelMessage]:
        return (await self.archive.load(session_id))[1]

    async def sessions(self, user: str | None = None) -> list[SessionMeta]:
        return await self.archive.list(user)

    async def owner(self, session_id: str) -> str | None:
        meta = await self.archive.load_meta(session_id)
        if meta is not None:
            return meta.user
        try:  # started, but no snapshot yet
            return await self._handle(session_id).query(SessionWorkflow.owner)
        except WorkflowQueryFailedError:
            return None  # not answerable yet; submits are still checked by the workflow itself
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            return None

    async def close_session(self, session_id: str) -> None:
        try:
            await self._handle(session_id).signal(SessionWorkflow.close)
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise


def _cause_type(exc: WorkflowUpdateFailedError) -> str | None:
    return getattr(exc.cause, "type", None)


def _is_closing(exc: WorkflowUpdateFailedError) -> bool:
    cause = exc.cause
    return isinstance(cause, ApplicationError) and cause.type == "SessionClosing"
