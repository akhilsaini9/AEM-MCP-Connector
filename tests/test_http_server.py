from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.http_server import create_http_app, resolve_http_host, resolve_http_port
from aem_mcp.server import mcp

TOKEN = "test-token-with-enough-entropy"
MCP_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def http_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mcp_http_bearer_token": TOKEN,
        "mcp_http_allowed_hosts": "testserver,localhost:*,127.0.0.1:*",
        "mcp_http_allowed_origins": "http://testserver",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


@pytest.mark.asyncio
async def test_stdio_tools_still_load() -> None:
    names = {tool.name for tool in await mcp.list_tools()}
    existing = {
        "get_page_properties",
        "search_pages",
        "list_child_pages",
        "find_component_usage",
        "create_page",
        "update_page_properties",
        "set_page_property",
        "move_page",
        "delete_page",
        "list_components",
        "get_component_properties",
        "find_components",
        "add_component",
        "update_component_properties",
    }
    new = {"publish_page", "unpublish_page", "get_page_dependencies", "validate_page", "search_assets", "get_asset_metadata", "get_asset_preview", "upload_asset", "find_asset_usage", "update_asset_metadata", "publish_asset", "unpublish_asset", "get_component_authoring_schema", "get_component_definition", "list_allowed_components"}
    assert existing <= names
    assert new <= names
    adobe_poc = {
        "get_adobe_mcp_connection_status",
        "connect_adobe_mcp",
        "disconnect_adobe_mcp",
        "list_adobe_mcp_tools",
        "list_aem_cloud_environments",
    }
    assert adobe_poc <= names
    assert "create_package" in names
    assert len(names) == 35


def test_http_tool_listing_includes_asset_preview() -> None:
    with TestClient(create_http_app(http_settings())) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=rpc("tools/list", {}, request_id=9))
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    preview = next(tool for tool in tools if tool["name"] == "get_asset_preview")
    assert set(preview["inputSchema"]["properties"]) == {"asset_path", "rendition", "max_bytes"}


def test_http_endpoint_starts_and_health_is_public() -> None:
    with TestClient(create_http_app(http_settings())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "transport": "streamable-http"}


def test_local_host_and_port_behavior_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    settings = http_settings(mcp_host="127.0.0.1", mcp_port=8123)
    assert resolve_http_host(settings) == "127.0.0.1"
    assert resolve_http_port(settings) == 8123


def test_cloud_run_port_takes_precedence_and_binds_all_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9090")
    settings = http_settings(mcp_host="127.0.0.1", mcp_port=8123)
    assert resolve_http_host(settings) == "0.0.0.0"
    assert resolve_http_port(settings) == 9090


def test_invalid_cloud_run_port_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(ValueError, match="PORT must be an integer"):
        create_http_app(http_settings())


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_missing_or_invalid_bearer_token_returns_401(authorization: str | None) -> None:
    headers = {"Content-Type": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    with TestClient(create_http_app(http_settings())) as client:
        response = client.post("/mcp", headers=headers, json=rpc("initialize", {}))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_valid_bearer_token_succeeds() -> None:
    initialize = rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    with TestClient(create_http_app(http_settings())) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=initialize)
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "custom-aem-crud"


def test_read_tool_works_through_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(self: AEMClient, page_path: str) -> dict[str, Any]:
        return {"path": page_path, "properties": {"jcr:title": "Mock page"}}

    monkeypatch.setattr(AEMClient, "get_page_properties", fake_get)
    call = rpc(
        "tools/call",
        {"name": "get_page_properties", "arguments": {"path": "/content/mock"}},
        request_id=2,
    )
    with TestClient(create_http_app(http_settings())) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=call)
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    assert "Mock page" in body["result"]["content"][0]["text"]


def test_component_tool_works_through_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list(self: AEMClient, page_path: str, max_depth: int = 10, limit: int = 200) -> dict[str, Any]:
        return {"pagePath": page_path, "components": [{"path": page_path + "/jcr:content/text", "name": "text", "resourceType": "sigma/components/content/text", "properties": {"text": "Mock"}}]}

    monkeypatch.setattr(AEMClient, "list_components", fake_list)
    call = rpc("tools/call", {"name": "list_components", "arguments": {"page_path": "/content/mock"}}, request_id=3)
    with TestClient(create_http_app(http_settings())) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=call)
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert "sigma/components/content/text" in response.json()["result"]["content"][0]["text"]


def test_write_guard_blocks_outside_root() -> None:
    settings = Settings(
        _env_file=None,
        aem_write_enabled=True,
        aem_write_roots="/content/mcp-poc",
    )
    client = AEMClient()
    client.settings = settings
    with pytest.raises(PermissionError, match="outside AEM_WRITE_ROOTS"):
        client._validate_write_path("/content/production/page")


@pytest.mark.asyncio
async def test_delete_requires_confirmation() -> None:
    client = AEMClient()
    client.settings = Settings(
        _env_file=None,
        aem_write_enabled=True,
        aem_write_roots="/content/mcp-poc",
    )
    with pytest.raises(PermissionError, match="confirm=true"):
        await client.delete_page("/content/mcp-poc/test", confirm=False)
