"""In-process executor with file-backed sessions in `<root>/var/lib/sessions/`.

Used by the CLI and tests, and in development. Production uses the Temporal executor, where each
session is an entity workflow and history snapshots go to MinIO (see `runtime/temporal/`).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from .runtime import Runtime

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class FileSessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, session_id: str) -> Path:
        if not _SAFE_ID.match(session_id):
            raise ValueError(f"invalid session id {session_id!r}")
        return self.directory / f"{session_id}.json"

    def load(self, session_id: str) -> list[ModelMessage]:
        path = self._path(session_id)
        return ModelMessagesTypeAdapter.validate_json(path.read_bytes()) if path.exists() else []

    def save(self, session_id: str, messages: list[ModelMessage]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(ModelMessagesTypeAdapter.dump_json(messages))
        tmp.replace(path)

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json")) if self.directory.exists() else []


class InlineExecutor:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.sessions = FileSessionStore(runtime.root.var_lib / "sessions")

    async def send(self, prompt: str, session_id: str | None = None, **run_kwargs: object) -> tuple[str, str]:
        """Run one turn in a session. Returns `(session_id, output)`."""
        session_id = session_id or uuid.uuid4().hex[:12]
        history = self.sessions.load(session_id)
        result = await self.runtime.run(prompt, message_history=history, session_id=session_id, **run_kwargs)  # type: ignore[arg-type]
        self.sessions.save(session_id, result.all_messages())
        return session_id, result.output
