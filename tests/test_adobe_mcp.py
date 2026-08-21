from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aem_mcp.adobe_mcp.auth import (
    InMemoryAdobeTokenStore,
    OAuthCallbackBroker,
    SessionTokenStorage,
)
from aem_mcp.adobe_mcp.client import AdobeMCPClient, sanitize_downstream
from aem_mcp.adobe_mcp.errors import (
    AdobeMCPAuthRequiredError,
    AdobeMCPToolNotAllowedError,
)
from aem_mcp.adobe_mcp.sessions import AdobeMCPSessionManager
from aem_mcp.config import Settings


def cloud_settings(**overrides: object) -> Settings:
    values = {
        "adobe_mcp_enabled": True,
        "adobe_mcp_single_developer_mode": True,
        "adobe_mcp_oauth_redirect_uri": "https://localhost/adobe-mcp/oauth/callback",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_adobe_mcp_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.adobe_mcp_enabled is False
    assert settings.adobe_mcp_allowed_tool_names == frozenset()
    assert settings.adobe_mcp_server_url == "https://mcp.adobeaemcloud.com/adobe/mcp/cloudmanager"


def test_allowlist_is_exact_and_trimmed() -> None:
    settings = cloud_settings(adobe_mcp_allowed_tools=" list-envs,read-program ")
    assert settings.adobe_mcp_allowed_tool_names == {"list-envs", "read-program"}
    assert "list-*" not in settings.adobe_mcp_allowed_tool_names


@pytest.mark.asyncio
async def test_in_memory_token_store_isolates_users() -> None:
    store = InMemoryAdobeTokenStore()
    await store.save("a", {"tokens": "token-a"})
    await store.save("b", {"tokens": "token-b"})
    assert (await store.get("a"))["tokens"] == "token-a"
    assert (await store.get("b"))["tokens"] == "token-b"
    await store.delete("a")
    assert await store.get("a") is None
    assert (await store.get("b"))["tokens"] == "token-b"


@pytest.mark.asyncio
async def test_sdk_storage_adapter_keeps_client_info_and_tokens_per_session() -> None:
    store = InMemoryAdobeTokenStore()
    a = SessionTokenStorage(store, "a")
    b = SessionTokenStorage(store, "b")
    await a.set_tokens("a-token")
    await a.set_client_info("a-client")
    await b.set_tokens("b-token")
    assert await a.get_tokens() == "a-token"
    assert await a.get_client_info() == "a-client"
    assert await b.get_tokens() == "b-token"
    assert await b.get_client_info() is None


@pytest.mark.asyncio
async def test_callback_state_is_one_use_and_routed_to_session() -> None:
    broker = OAuthCallbackBroker()
    future = await broker.register("user-a", "https://ims.example/authorize?state=opaque-a")
    assert await broker.complete(code="safe-code", state="opaque-a", iss="https://issuer") == "user-a"
    result = await future
    assert result.code == "safe-code"
    with pytest.raises(Exception, match="invalid or was already used"):
        await broker.complete(code="replay", state="opaque-a", iss=None)


@pytest.mark.asyncio
async def test_callback_rejects_missing_state() -> None:
    broker = OAuthCallbackBroker()
    with pytest.raises(Exception, match="did not contain OAuth state"):
        await broker.register("user-a", "https://ims.example/authorize")


def test_sanitizer_removes_secrets_recursively() -> None:
    value = {
        "access_token": "secret",
        "nested": {"Authorization": "Bearer secret", "safe": "ok"},
        "items": [{"refresh_token": "secret", "name": "visible"}],
    }
    assert sanitize_downstream(value) == {
        "nested": {"safe": "ok"},
        "items": [{"name": "visible"}],
    }


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_call_tool() -> None:
    client = AdobeMCPClient(cloud_settings(), "a", InMemoryAdobeTokenStore())
    client._session = SimpleNamespace()
    with pytest.raises(AdobeMCPToolNotAllowedError):
        await client.call_tool("anything")


@pytest.mark.asyncio
async def test_allowed_tool_invokes_downstream_and_sanitizes() -> None:
    class FakeSession:
        async def call_tool(self, name: str, arguments: dict[str, object]):
            return {"name": name, "arguments": arguments, "access_token": "never-return"}

    client = AdobeMCPClient(
        cloud_settings(adobe_mcp_allowed_tools="list-envs"),
        "a",
        InMemoryAdobeTokenStore(),
    )
    client._session = FakeSession()
    result = await client.call_tool("list-envs", {"limit": 2})
    assert result == {"name": "list-envs", "arguments": {"limit": 2}}


@pytest.mark.asyncio
async def test_tool_discovery_returns_only_name_and_description() -> None:
    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="read", description="Safe", inputSchema={"secret": True})])

    client = AdobeMCPClient(cloud_settings(), "a", InMemoryAdobeTokenStore())
    client._session = FakeSession()
    assert await client.list_tools() == [{"name": "read", "description": "Safe"}]
    assert client.tool_count == 1


@pytest.mark.asyncio
async def test_disconnect_one_session_does_not_delete_another() -> None:
    manager = AdobeMCPSessionManager(cloud_settings())
    await manager.token_store.save("a", {"tokens": "a"})
    await manager.token_store.save("b", {"tokens": "b"})
    manager._clients["a"] = SimpleNamespace(close=lambda: asyncio.sleep(0))
    manager.session_key = lambda: "a"  # type: ignore[method-assign]
    await manager.disconnect()
    assert await manager.token_store.get("a") is None
    assert (await manager.token_store.get("b"))["tokens"] == "b"


def test_trusted_identity_is_required_without_developer_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AdobeMCPSessionManager(cloud_settings(adobe_mcp_single_developer_mode=False))
    monkeypatch.setattr("aem_mcp.adobe_mcp.sessions.get_access_token", lambda: None)
    with pytest.raises(AdobeMCPAuthRequiredError):
        manager.session_key()


def test_authenticated_subject_becomes_session_key(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AdobeMCPSessionManager(cloud_settings(adobe_mcp_single_developer_mode=False))
    monkeypatch.setattr(
        "aem_mcp.adobe_mcp.sessions.get_access_token",
        lambda: SimpleNamespace(subject="stable-user-id"),
    )
    assert manager.session_key() == "mcp-subject:stable-user-id"
