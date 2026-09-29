import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ladderframe.config.schema import JWTConfig, ServerConfig
from ladderframe.server.auth import Authenticator


def app_for(config: ServerConfig) -> TestClient:
    authenticate = Authenticator(config)
    app = FastAPI()

    @app.get("/me")
    async def me(request: Request) -> dict[str, str | None]:
        return {"user": (await authenticate(request)).user}

    return TestClient(app)


def token(key: object, algorithm: str, **claims: object) -> str:
    payload = {"sub": "ada", "exp": int(time.time()) + 60, "iss": "https://idp", "aud": "ladderframe", **claims}
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": "k1"})


def test_hs256_secret() -> None:
    config = ServerConfig(
        auth="jwt", jwt=JWTConfig(secret="s" * 32, algorithms=["HS256"], issuer="https://idp", audience="ladderframe")
    )
    client = app_for(config)
    good = token("s" * 32, "HS256")
    assert client.get("/me", headers={"Authorization": f"Bearer {good}"}).json() == {"user": "ada"}
    assert client.get("/me").status_code == 401
    expired = token("s" * 32, "HS256", exp=int(time.time()) - 3600)
    assert client.get("/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    other_audience = token("s" * 32, "HS256", aud="someone-else")
    assert client.get("/me", headers={"Authorization": f"Bearer {other_audience}"}).status_code == 401
    wrong_key = token("x" * 32, "HS256")
    assert client.get("/me", headers={"Authorization": f"Bearer {wrong_key}"}).status_code == 401


@pytest.fixture
def jwks_server() -> Iterator[tuple[str, rsa.RSAPrivateKey]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    body = json.dumps({"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/jwks.json", private_key
    server.shutdown()


def test_rs256_via_jwks(jwks_server: tuple[str, rsa.RSAPrivateKey]) -> None:
    url, private_key = jwks_server
    config = ServerConfig(
        auth="jwt", jwt=JWTConfig(jwks_url=url, issuer="https://idp", audience="ladderframe", user_claim="email")
    )
    client = app_for(config)
    good = token(private_key, "RS256", email="ada@example.com")
    assert client.get("/me", headers={"Authorization": f"Bearer {good}"}).json() == {"user": "ada@example.com"}
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = token(other_key, "RS256")
    assert client.get("/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_token_without_user_claim_is_rejected() -> None:
    config = ServerConfig(auth="jwt", jwt=JWTConfig(secret="s" * 32, algorithms=["HS256"], user_claim="email"))
    client = app_for(config)
    no_email = jwt.encode({"sub": "svc", "exp": int(time.time()) + 60}, "s" * 32, algorithm="HS256")
    response = client.get("/me", headers={"Authorization": f"Bearer {no_email}"})
    assert response.status_code == 401 and "email" in response.json()["detail"]


def test_jwt_config_needs_one_key_source() -> None:
    with pytest.raises(ValueError):
        Authenticator(ServerConfig(auth="jwt", jwt=JWTConfig()))
    with pytest.raises(ValueError):
        Authenticator(ServerConfig(auth="jwt", jwt=JWTConfig(secret="a", public_key="b")))


def test_api_key_mode_uses_forwarded_user() -> None:
    client = app_for(ServerConfig(auth="api-key", api_keys=["k"]))
    assert client.get("/me", headers={"X-API-Key": "k", "X-User-Id": "bob"}).json() == {"user": "bob"}
