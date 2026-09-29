"""FastAPI app for one agent (`ladderframe serve`).

    GET    /health                                   liveness
    GET    /ready                                    executor + object storage reachable
    GET    /v1/sessions                              list sessions (filtered by X-User-Id)
    POST   /v1/sessions                              new session id
    POST   /v1/sessions/{id}/messages                send a message: wait (default), async (202), or SSE stream
    GET    /v1/sessions/{id}/messages                history (pydantic-ai messages JSON)
    GET    /v1/sessions/{id}/turns/{turn}            turn status
    GET    /v1/sessions/{id}/turns/{turn}/events     turn events as SSE
    DELETE /v1/sessions/{id}                         close the session
    POST   /chat                                     Vercel AI SDK data stream  (protocols: vercel-ai)
    POST   /ag-ui                                    AG-UI                      (protocols: ag-ui)
    GET    /                                         pydantic-ai chat UI, development only (protocols: web)

Every protocol goes through the executor, so with the Temporal executor all of them are durable —
except the `web` UI, which runs the agent directly and is meant for local development: it has no
authentication, so it is refused unless `server.auth` is `none`.

A session belongs to the user who started it; other users get 404 for it, as if it didn't exist.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import AgentStreamEvent, ModelMessagesTypeAdapter, ModelRequest, UserPromptPart
from pydantic_ai.run import AgentRunResultEvent

from ..observability import instrument_app, metrics
from ..observability.metrics import HTTPMetricsMiddleware
from ..runtime.executor import Executor, NativeEvent, NotOwner, TurnFailed, TurnState, new_id
from ..runtime.runtime import Runtime
from ..storage.sessions import SessionMeta, validate_session_id
from .auth import Authenticator, Principal

logger = logging.getLogger("ladderframe.server")

_EVENT_ADAPTER: TypeAdapter[AgentStreamEvent] = TypeAdapter(AgentStreamEvent)


async def _principal(request: Request) -> Principal:
    return await request.app.state.authenticate(request)


Auth = Annotated[Principal, Depends(_principal)]


class MessageRequest(BaseModel):
    prompt: str = Field(min_length=1)
    mode: Literal["wait", "async", "stream"] = "wait"


class SessionCreated(BaseModel):
    session_id: str


def create_app(
    runtime: Runtime,
    executor: Executor,
    *,
    background: Callable[[], Awaitable[AsyncExitStack]] | None = None,
) -> FastAPI:
    """`background` starts extra services (the Temporal worker) for the app's lifetime."""
    authenticate = Authenticator(runtime.config.server)
    protocols = set(runtime.config.server.protocols)
    if error := runtime.config.server.web_protocol_error():
        raise ValueError(error)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await executor.start()
        stack = await background() if background else AsyncExitStack()
        async with stack:
            try:
                yield
            finally:
                await executor.aclose()

    app = FastAPI(title=f"ladderframe: {runtime.name}", lifespan=lifespan)
    app.add_middleware(HTTPMetricsMiddleware)
    instrument_app(runtime, app)
    app.state.runtime, app.state.executor, app.state.authenticate = runtime, executor, authenticate

    def session_id_or_404(session_id: str) -> str:
        try:
            return validate_session_id(session_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    async def owned_session(session_id: str, principal: Principal) -> str:
        """A valid session id that isn't another user's (404 either way, so ids aren't revealed)."""
        session_id = session_id_or_404(session_id)
        owner = await executor.owner(session_id)
        if owner is not None and owner != principal.user:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
        return session_id

    async def submit(session_id: str, prompt: str, principal: Principal) -> TurnState:
        try:
            return await executor.submit(session_id, prompt, user=principal.user)
        except NotOwner as exc:  # another user claimed the id since `owned_session` looked
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": runtime.name}

    @app.get("/ready")
    async def ready() -> Response:
        checks: dict[str, str] = {}
        try:
            await runtime.object_store.ping()
            checks["object_store"] = "ok"
        except Exception as exc:  # noqa: BLE001 — details go to the log, not to anonymous callers
            logger.warning("object store not ready: %s", exc)
            checks["object_store"] = "error"
        client = getattr(executor, "client", None)
        if client is not None:
            try:
                await client.service_client.check_health()
                checks["temporal"] = "ok"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal not ready: %s", exc)
                checks["temporal"] = "error"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ok, **checks}, status_code=200 if ok else 503)

    if runtime.config.observability.metrics:

        @app.get("/metrics", include_in_schema=False)
        async def prometheus() -> Response:
            body, content_type = metrics.exposition()
            return Response(body, media_type=content_type)

    @app.get("/v1/sessions")
    async def list_sessions(principal: Auth) -> list[SessionMeta]:
        return await executor.sessions(principal.user)

    @app.post("/v1/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(principal: Auth) -> SessionCreated:
        return SessionCreated(session_id=new_id())

    @app.post("/v1/sessions/{session_id}/messages", response_model=None)
    async def send_message(session_id: str, body: MessageRequest, principal: Auth) -> TurnState | Response:
        session_id = await owned_session(session_id, principal)
        turn = await submit(session_id, body.prompt, principal)
        if body.mode == "async":
            return JSONResponse(turn.model_dump(), status_code=status.HTTP_202_ACCEPTED)
        if body.mode == "stream":
            return _sse(executor.events(session_id, turn.turn_id), turn)
        return await executor.wait(session_id, turn.turn_id)

    @app.get("/v1/sessions/{session_id}/messages")
    async def history(session_id: str, principal: Auth) -> Response:
        messages = await executor.history(await owned_session(session_id, principal))
        return Response(ModelMessagesTypeAdapter.dump_json(messages), media_type="application/json")

    @app.get("/v1/sessions/{session_id}/turns/{turn_id}")
    async def turn_status(session_id: str, turn_id: str, principal: Auth) -> TurnState:
        try:
            return await executor.turn(await owned_session(session_id, principal), turn_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @app.get("/v1/sessions/{session_id}/turns/{turn_id}/events")
    async def turn_events(session_id: str, turn_id: str, principal: Auth) -> Response:
        session_id = await owned_session(session_id, principal)
        return _sse(executor.events(session_id, turn_id), None)

    @app.delete("/v1/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def close(session_id: str, principal: Auth) -> None:
        await executor.close_session(await owned_session(session_id, principal))

    if "vercel-ai" in protocols or "web" in protocols:
        from pydantic_ai.ui.vercel_ai import VercelAIAdapter

        @app.post("/chat", response_model=None)
        async def vercel_chat(request: Request, principal: Auth) -> Response:
            return await _ui_turn(VercelAIAdapter, request, principal)

    if "ag-ui" in protocols:
        from pydantic_ai.ui.ag_ui import AGUIAdapter

        @app.post("/ag-ui", response_model=None)
        async def ag_ui(request: Request, principal: Auth) -> Response:
            return await _ui_turn(AGUIAdapter, request, principal)

    async def _ui_turn(adapter_cls: Any, request: Request, principal: Principal) -> Response:
        """UI protocols send the whole conversation; the session keeps its own history, so only the
        latest user message is submitted, to the session named by the chat / thread id."""
        adapter = await adapter_cls.from_request(request, agent=runtime.agent)
        session_id = await owned_session(adapter.conversation_id or new_id(), principal)
        prompt = _last_user_text(adapter.messages)
        if not prompt:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no user message in the request")
        turn = await submit(session_id, prompt, principal)
        return adapter.streaming_response(adapter.transform_stream(_native(executor.events(session_id, turn.turn_id))))

    if "web" in protocols:
        # pydantic-ai's chat UI runs the agent directly (not through the executor): development only.
        try:
            app.mount("/", runtime.agent.to_web(deps=runtime.new_deps("web-ui")))
        except UserError as exc:  # e.g. no API key yet: keep the API up, skip the dev UI
            logger.warning("web UI disabled: %s", exc)

    return app


def _last_user_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, UserPromptPart):
                    if isinstance(part.content, str):
                        return part.content
                    return "\n".join(c for c in part.content if isinstance(c, str))
    return ""


async def _native(events: AsyncIterator[NativeEvent]) -> AsyncIterator[NativeEvent]:
    try:
        async for event in events:
            yield event
    except TurnFailed:
        return


def _sse(events: AsyncIterator[NativeEvent], turn: TurnState | None) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        if turn is not None:
            yield _frame("turn", turn.model_dump_json())
        try:
            async for event in events:
                if isinstance(event, AgentRunResultEvent):
                    yield _frame("result", json.dumps({"output": event.result.output}))
                else:
                    yield _frame(event.event_kind, _EVENT_ADAPTER.dump_json(event).decode())
        except TurnFailed as exc:
            yield _frame("error", json.dumps({"error": str(exc)}))

    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _frame(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"
