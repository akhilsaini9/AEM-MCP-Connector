from __future__ import annotations

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from aem_mcp.adobe_cloud.auth import AdobeCloudOAuth, AdobeTokenResponse
from aem_mcp.adobe_cloud.client import AdobeCloudClient
from aem_mcp.adobe_cloud.errors import AdobeCloudAuthenticationError, AdobeCloudOAuthStateError, AdobeCloudPermissionError, AdobeCloudTokenExchangeError, AdobeCloudUserInfoError
from aem_mcp.adobe_cloud.sessions import AdobeCloudSession, AdobeCloudSessionManager, MemoryAdobeCloudSessionStore, PendingOAuthState
from aem_mcp.config import Settings
from aem_mcp.http_server import create_http_app


def settings(**overrides: object) -> Settings:
    values = {"adobe_cloud_enabled": True, "adobe_cloud_client_id": "client", "adobe_cloud_client_secret": "secret", "adobe_cloud_scopes": "openid, AdobeID"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def response(status: int, data: object) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://example.test"))


class FakeHTTP:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, object]]] = []
    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append((method, url, kwargs)); return self.responses.pop(0)
    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append(("GET", url, kwargs)); return self.responses.pop(0)


def test_configuration_disabled() -> None:
    assert Settings(_env_file=None).adobe_cloud_enabled is False


@pytest.mark.parametrize("field", ["adobe_cloud_client_id", "adobe_cloud_client_secret"])
def test_enabled_requires_credentials(field: str) -> None:
    with pytest.raises(ValidationError): settings(**{field: ""})


def test_invalid_redirect_and_endpoint_rejected() -> None:
    with pytest.raises(ValidationError): settings(adobe_cloud_redirect_uri="http://public.example/callback")
    with pytest.raises(ValidationError): settings(adobe_cloud_token_endpoint="http://example.test/token")


@pytest.mark.asyncio
async def test_sessions_are_isolated_and_disconnect_is_scoped() -> None:
    store = MemoryAdobeCloudSessionStore()
    await store.save(AdobeCloudSession("a", access_token="token-a")); await store.save(AdobeCloudSession("b", access_token="token-b"))
    manager = AdobeCloudSessionManager(settings(), store=store); manager.session_key = lambda: "a"  # type: ignore[method-assign]
    await manager.disconnect()
    assert await store.get("a") is None and (await store.get("b")).access_token == "token-b"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_authorization_url_has_bound_secure_state() -> None:
    store = MemoryAdobeCloudSessionStore(); oauth = AdobeCloudOAuth(settings(), store)
    url = await oauth.authorization_url("user-a", now=100)
    query = parse_qs(urlsplit(url).query); state = query["state"][0]
    assert query["client_id"] == ["client"] and query["response_type"] == ["code"] and "client_secret" not in query
    assert await store.consume_state(state, 101) == "user-a"
    with pytest.raises(AdobeCloudOAuthStateError): await store.consume_state(state, 101)


@pytest.mark.asyncio
async def test_state_invalid_expired_and_replayed() -> None:
    store = MemoryAdobeCloudSessionStore()
    with pytest.raises(AdobeCloudOAuthStateError): await store.consume_state("missing", time.time())
    await store.save_state("expired", PendingOAuthState("a", 1))
    with pytest.raises(AdobeCloudOAuthStateError): await store.consume_state("expired", 2)


@pytest.mark.asyncio
async def test_token_exchange_success_and_optional_refresh() -> None:
    fake = FakeHTTP([response(200, {"access_token": "access", "expires_in": "3600", "token_type": "Bearer"})])
    token = await AdobeCloudOAuth(settings(), MemoryAdobeCloudSessionStore(), client=fake).exchange_code("code")
    assert token.access_token == "access" and token.expires_in == 3600 and token.refresh_token is None
    form = fake.requests[0][2]["data"]; assert form["client_secret"] == "secret" and form["code"] == "code"  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,data", [(400, {"error": "invalid_grant"}), (200, {}), (200, {"access_token": "a", "expires_in": 0}), (200, [])])
async def test_token_exchange_rejects_http_or_invalid_response(status: int, data: object) -> None:
    oauth = AdobeCloudOAuth(settings(), MemoryAdobeCloudSessionStore(), client=FakeHTTP([response(status, data)]))
    with pytest.raises(AdobeCloudTokenExchangeError): await oauth.exchange_code("code")


@pytest.mark.asyncio
async def test_userinfo_success_401_and_malformed() -> None:
    oauth = AdobeCloudOAuth(settings(), MemoryAdobeCloudSessionStore(), client=FakeHTTP([response(200, {"sub": "adobe-user", "email": "safe@example.test"})]))
    assert (await oauth.userinfo("token"))["subject"] == "adobe-user"
    for item in [response(401, {}), response(200, {"email": "missing-sub"})]:
        oauth = AdobeCloudOAuth(settings(), MemoryAdobeCloudSessionStore(), client=FakeHTTP([item]))
        with pytest.raises(AdobeCloudUserInfoError): await oauth.userinfo("token")


@pytest.mark.asyncio
async def test_status_disabled_disconnected_connected_and_expired() -> None:
    disabled = AdobeCloudSessionManager(Settings(_env_file=None))
    assert (await disabled.status())["session_state"] == "disabled"
    store = MemoryAdobeCloudSessionStore(); manager = AdobeCloudSessionManager(settings(), store=store); manager.session_key = lambda: "a"  # type: ignore[method-assign]
    assert (await manager.status())["session_state"] == "disconnected"
    await store.save(AdobeCloudSession("a", access_token="x", expires_at=time.time()+100, adobe_user_id="id", connected_at="now"))
    assert (await manager.status())["connected"] is True
    await store.save(AdobeCloudSession("a", access_token="x", expires_at=1, adobe_user_id="id"))
    assert (await manager.status())["connected"] is False


class FakeOAuth:
    def __init__(self, token: AdobeTokenResponse | Exception) -> None: self.token = token
    async def refresh(self, _: str) -> AdobeTokenResponse:
        if isinstance(self.token, Exception): raise self.token
        return self.token


@pytest.mark.asyncio
async def test_refresh_success_failure_missing_token_and_no_refresh() -> None:
    store = MemoryAdobeCloudSessionStore(); await store.save(AdobeCloudSession("a", access_token="old", refresh_token="refresh", expires_at=1))
    manager = AdobeCloudSessionManager(settings(), store=store, oauth=FakeOAuth(AdobeTokenResponse("new", 100)))
    assert await manager.get_valid_access_token("a") == "new"
    await store.save(AdobeCloudSession("a", access_token="old", refresh_token="refresh", expires_at=1))
    manager.oauth = FakeOAuth(AdobeCloudTokenExchangeError())
    with pytest.raises(AdobeCloudAuthenticationError): await manager.get_valid_access_token("a")
    assert await store.get("a") is None
    await store.save(AdobeCloudSession("a", access_token="old", expires_at=1))
    with pytest.raises(AdobeCloudAuthenticationError): await manager.get_valid_access_token("a")


@pytest.mark.asyncio
async def test_callback_success_oauth_error_token_and_userinfo_failures() -> None:
    class CallbackOAuth:
        def __init__(self, failure: str | None = None): self.failure = failure
        async def exchange_code(self, code: str):
            if self.failure == "token": raise AdobeCloudTokenExchangeError()
            return AdobeTokenResponse("token", 100)
        async def userinfo(self, *_: object):
            if self.failure == "userinfo": raise AdobeCloudUserInfoError()
            return {"subject": "id", "email": None}
        async def establish_session(self, key: str, token: AdobeTokenResponse, identity: object, author: str): return None
    for failure in [None, "token", "userinfo"]:
        store = MemoryAdobeCloudSessionStore(); await store.save_state("state", PendingOAuthState("user", time.time()+10))
        manager = AdobeCloudSessionManager(settings(), store=store, oauth=CallbackOAuth(failure))
        if failure:
            with pytest.raises(Exception): await manager.complete_callback(code="code", state="state")
        else: await manager.complete_callback(code="code", state="state")
    store = MemoryAdobeCloudSessionStore(); await store.save_state("state", PendingOAuthState("user", time.time()+10))
    with pytest.raises(AdobeCloudAuthenticationError): await AdobeCloudSessionManager(settings(), store=store, oauth=CallbackOAuth()).complete_callback(code="", state="state", oauth_error="access_denied")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(200, True), (404, None), (500, False)])
async def test_probe_safe_statuses(status: int, expected: bool | None) -> None:
    client = AdobeCloudClient(settings(aem_cloud_author_url="https://author.example", aem_cloud_test_path="/supported"), client=FakeHTTP([response(status, {"secret": "never returned"})]))
    result = await client.probe("token"); assert result["authorized"] is expected and "secret" not in result


@pytest.mark.asyncio
async def test_probe_missing_configuration_401_and_403() -> None:
    result = await AdobeCloudClient(settings()).probe("token"); assert result["author_url_configured"] is False
    result = await AdobeCloudClient(settings(aem_cloud_author_url="https://author.example")).probe("token"); assert result["status"] == "oauth_connected_probe_not_configured"
    for status, exc in [(401, AdobeCloudAuthenticationError), (403, AdobeCloudPermissionError)]:
        client = AdobeCloudClient(settings(aem_cloud_author_url="https://author.example", aem_cloud_test_path="/supported"), client=FakeHTTP([response(status, {})]))
        with pytest.raises(exc): await client.probe("token")


def test_callback_success_page_is_no_store_and_contains_no_oauth_values(monkeypatch: pytest.MonkeyPatch) -> None:
    async def complete_callback(**_: object) -> None: return None
    monkeypatch.setattr("aem_mcp.http_server.adobe_cloud_sessions.complete_callback", complete_callback)
    app_settings = Settings(_env_file=None, mcp_http_bearer_token="test-token", mcp_http_allowed_hosts="testserver")
    with TestClient(create_http_app(app_settings)) as client:
        result = client.get("/adobe-cloud/oauth/callback?code=sensitive-code&state=sensitive-state")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert "sensitive-code" not in result.text and "sensitive-state" not in result.text and "token" not in result.text.lower()
