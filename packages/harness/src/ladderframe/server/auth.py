"""Request authentication.

server.auth: none      no checks (development); `X-User-Id` names the user
server.auth: api-key   `Authorization: Bearer <key>` or `X-API-Key: <key>`, compared in constant time;
                       the calling service may name the end user in `X-User-Id`
server.auth: jwt       `Authorization: Bearer <jwt>` verified against `server.jwt` (JWKS URL, PEM key or
                       shared secret; issuer, audience, expiry); the user comes from `server.jwt.user_claim`
"""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, status

from ..config.schema import JWTConfig, ServerConfig


@dataclass
class Principal:
    user: str | None
    claims: dict[str, Any] = field(default_factory=dict)


class Authenticator:
    def __init__(self, config: ServerConfig) -> None:
        self.mode = config.auth
        self.keys = [k.encode() for k in config.api_keys if k]
        self.jwt: _JWTVerifier | None = None
        if self.mode == "api-key" and not self.keys:
            raise ValueError("server.auth is api-key but server.api_keys is empty (is LADDERFRAME_API_KEY set?)")
        if self.mode == "jwt":
            self.jwt = _JWTVerifier(config.jwt)

    async def __call__(self, request: Request) -> Principal:
        if self.mode == "none":
            return Principal(request.headers.get("x-user-id") or None)
        if self.jwt is not None:
            token = _bearer(request.headers.get("authorization"))
            if not token:
                raise _unauthorized("missing bearer token")
            return await self.jwt.verify(token)
        supplied = request.headers.get("x-api-key") or _bearer(request.headers.get("authorization"))
        if not supplied or not any(hmac.compare_digest(supplied.encode(), key) for key in self.keys):
            raise _unauthorized("invalid or missing API key")
        return Principal(request.headers.get("x-user-id") or None)


class _JWTVerifier:
    def __init__(self, config: JWTConfig) -> None:
        try:
            import jwt
        except ImportError as exc:
            raise ValueError("server.auth: jwt needs `pip install 'ladderframe[auth]'`") from exc
        sources = [s for s in (config.jwks_url, config.public_key, config.secret) if s]
        if len(sources) != 1:
            raise ValueError("server.jwt needs exactly one of jwks_url, public_key or secret")
        self.config = config
        self._jwt = jwt
        self._jwks = jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=3600) if config.jwks_url else None

    def _key(self, token: str) -> Any:
        if self._jwks is not None:
            return self._jwks.get_signing_key_from_jwt(token).key  # cached; fetches JWKS on key rotation
        return self.config.public_key or self.config.secret

    async def verify(self, token: str) -> Principal:
        config = self.config
        try:
            key = await asyncio.to_thread(self._key, token)
            claims = self._jwt.decode(
                token,
                key,
                algorithms=config.algorithms,
                audience=config.audience,
                issuer=config.issuer,
                leeway=config.leeway_seconds,
                options={
                    "require": ["exp"],
                    "verify_aud": config.audience is not None,
                    "verify_iss": config.issuer is not None,
                },
            )
        except self._jwt.PyJWTError as exc:
            raise _unauthorized(f"invalid token: {exc}") from exc
        user = claims.get(config.user_claim)
        if user is None or user == "":  # a user-less token would see every user's sessions
            raise _unauthorized(f"token has no {config.user_claim!r} claim")
        return Principal(str(user), claims)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None
