"""Request authentication.

    server.auth: none      no checks (development)
    server.auth: api-key   `Authorization: Bearer <key>` or `X-API-Key: <key>`, compared in constant time

With an API key, the caller may name the end user in `X-User-Id`; sessions are then filtered by it.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from ..config.schema import ServerConfig


@dataclass
class Principal:
    user: str | None


class Authenticator:
    def __init__(self, config: ServerConfig) -> None:
        self.mode = config.auth
        self.keys = [k.encode() for k in config.api_keys if k]
        if self.mode == "api-key" and not self.keys:
            raise ValueError("server.auth is api-key but server.api_keys is empty (is LADDERFRAME_API_KEY set?)")
        if self.mode == "jwt":
            raise ValueError("server.auth: jwt is not implemented yet; use api-key behind your gateway")

    async def __call__(self, request: Request) -> Principal:
        user = request.headers.get("x-user-id") or None
        if self.mode == "none":
            return Principal(user)
        supplied = request.headers.get("x-api-key") or _bearer(request.headers.get("authorization"))
        if not supplied or not any(hmac.compare_digest(supplied.encode(), key) for key in self.keys):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")
        return Principal(user)


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None
