from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from aem_mcp.config import Settings
from aem_mcp.http_server import create_http_app
from aem_mcp.oauth import GoogleOIDCTokenVerifier, OAuthValidationError


ISSUER = "https://accounts.google.com"
AUDIENCE = "google-client-id"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()


def settings(mode: str = "oauth", **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mcp_auth_mode": mode,
        "mcp_http_auth_enabled": False,
        "mcp_oauth_client_id": AUDIENCE,
        "mcp_public_base_url": "https://aem-mcp-connector.onrender.com",
        "mcp_http_allowed_hosts": "testserver",
        "mcp_http_allowed_origins": "http://testserver",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def signed_token(**overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "azp": AUDIENCE,
        "sub": "google-subject-123",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 300,
        "email": "person@example.com",
        "email_verified": True,
        "name": "Person",
        "scope": "openid email profile",
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


def verifier(config: Settings) -> GoogleOIDCTokenVerifier:
    async def discovery() -> dict[str, Any]:
        return {"issuer": ISSUER, "jwks_uri": "https://example.invalid/jwks"}

    async def signing_key(_: str, __: str) -> Any:
        return PUBLIC_KEY

    async def opaque(_: str) -> dict[str, Any]:
        raise OAuthValidationError("malformed_token")

    return GoogleOIDCTokenVerifier(
        config,
        discovery_loader=discovery,
        signing_key_loader=signing_key,
        opaque_token_loader=opaque,
    )


def rpc() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}


def test_protected_resource_metadata() -> None:
    with TestClient(create_http_app(settings(), oauth_verifier=verifier(settings()))) as client:
        response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    assert response.json()["resource"] == "https://aem-mcp-connector.onrender.com/mcp"
    assert response.json()["authorization_servers"] == [ISSUER]


def test_oauth_missing_token_has_resource_metadata_challenge() -> None:
    with TestClient(create_http_app(settings(), oauth_verifier=verifier(settings()))) as client:
        response = client.post("/mcp", json=rpc())
    assert response.status_code == 401
    assert 'resource_metadata="https://aem-mcp-connector.onrender.com/.well-known/oauth-protected-resource"' in response.headers["www-authenticate"]


@pytest.mark.parametrize(
    ("token", "error"),
    [
        ("not-a-jwt", "malformed_token"),
        (signed_token(iss="https://evil.example"), "invalid_issuer"),
        (signed_token(aud="wrong"), "invalid_audience"),
        (signed_token(exp=int(time.time()) - 120), "expired_token"),
    ],
)
def test_invalid_oauth_tokens(token: str, error: str) -> None:
    config = settings()
    with TestClient(create_http_app(config, oauth_verifier=verifier(config))) as client:
        response = client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json=rpc())
    assert response.status_code == 401
    assert response.json()["error_code"] == error


def test_valid_token_and_stable_subject_extraction() -> None:
    config = settings()
    with TestClient(create_http_app(config, oauth_verifier=verifier(config))) as client:
        response = client.post("/mcp", headers={"Authorization": f"Bearer {signed_token()}"}, json=rpc())
    assert response.status_code == 200

    # Verify the same trusted AccessToken shape consumed by Adobe session_key().
    import asyncio
    access = asyncio.run(verifier(config).verify(signed_token()))
    assert access.subject == "google-subject-123"
    assert access.token == ""


def test_valid_google_opaque_access_token() -> None:
    config = settings()

    async def opaque(_: str) -> dict[str, Any]:
        return {
            "audience": AUDIENCE,
            "expires_in": 300,
            "scope": "openid email profile",
            "sub": "opaque-google-subject",
            "email": "person@example.com",
            "email_verified": True,
        }

    oidc = GoogleOIDCTokenVerifier(config, opaque_token_loader=opaque)
    import asyncio
    access = asyncio.run(oidc.verify("opaque-access-token"))
    assert access.subject == "opaque-google-subject"
    assert access.token == ""


def test_health_public_in_oauth_mode() -> None:
    config = settings()
    with TestClient(create_http_app(config, oauth_verifier=verifier(config))) as client:
        assert client.get("/health").status_code == 200


def test_no_auth_mode_still_works() -> None:
    with TestClient(create_http_app(settings("none"))) as client:
        assert client.post("/mcp", json=rpc()).status_code == 200


def test_adobe_session_identity_is_not_google_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from aem_mcp.adobe_mcp.sessions import AdobeMCPSessionManager
    from aem_mcp.oauth import authenticated_context, reset_authenticated_context

    trusted = AccessToken(token="", client_id=AUDIENCE, scopes=["openid"], subject="subject-a", claims={"iss": ISSUER})
    context_token = authenticated_context(trusted)
    try:
        manager = AdobeMCPSessionManager(settings(adobe_mcp_single_developer_mode=False))
        assert manager.session_key() == "mcp-subject:subject-a"
        assert get_access_token().token == ""
    finally:
        reset_authenticated_context(context_token)
