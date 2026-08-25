from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aem_mcp.aem_client import AEMClient
from aem_mcp.cloud_tokens import LocalDevelopmentTokenProvider
from aem_mcp.config import Settings
from aem_mcp.http_transport import AEMHttpTransport, BearerTokenAuth
from aem_mcp.providers.direct_cloud import DirectAEMCloudProvider
from aem_mcp.providers.factory import get_aem_provider, get_repository_aem_client


def direct(**values: Any) -> Settings:
    base = dict(aem_runtime_mode="cloud", aem_cloud_provider_mode="direct_http",
                aem_cloud_author_url="https://author-p1-e2.adobeaemcloud.com",
                aem_cloud_local_token="test-token-not-real")
    base.update(values)
    return Settings(_env_file=None, **base)


def test_direct_configuration_and_factory_are_independent_of_openapi_oauth() -> None:
    settings = direct(adobe_cloud_enabled=False, adobe_cloud_client_id="", adobe_cloud_client_secret="")
    assert isinstance(get_aem_provider(settings), DirectAEMCloudProvider)
    client = get_repository_aem_client(settings)
    assert client.base_url == "https://author-p1-e2.adobeaemcloud.com"
    assert client.transport.verify is True
    assert client.transport.follow_redirects is False
    assert client.transport.strict_csrf is True


@pytest.mark.parametrize("values", [
    {"aem_cloud_local_token": ""},
    {"aem_cloud_author_url": "http://author.example"},
    {"aem_cloud_author_url": "https://author.example/path"},
])
def test_direct_configuration_rejects_missing_secret_or_unsafe_origin(values: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        direct(**values)


@pytest.mark.asyncio
async def test_bearer_auth_is_applied_without_basic_credentials() -> None:
    seen: dict[str, str] = {}
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})
    provider = LocalDevelopmentTokenProvider(direct())
    async with httpx.AsyncClient(auth=BearerTokenAuth(provider), transport=httpx.MockTransport(handler)) as client:
        await client.get("https://author.example/content/test.json")
    assert seen["authorization"] == "Bearer test-token-not-real"
    assert not seen["authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_strict_csrf_missing_token_blocks_write() -> None:
    settings = direct()
    transport = AEMHttpTransport.for_cloud_direct(settings, LocalDevelopmentTokenProvider(settings))
    client = AEMClient(transport, settings)

    class FakeHTTP:
        async def get(self, _: str) -> httpx.Response:
            return httpx.Response(200, json={}, request=httpx.Request("GET", "https://author.example/csrf"))

    with pytest.raises(RuntimeError, match="cloud_direct_csrf_token_missing"):
        await client._csrf_headers(FakeHTTP())  # type: ignore[arg-type]


def test_local_transport_remains_basic_and_tolerant() -> None:
    settings = Settings(_env_file=None)
    transport = AEMHttpTransport.local(settings)
    assert transport.auth == (settings.aem_username, settings.aem_password)
    assert transport.strict_csrf is False
    assert transport.follow_redirects is True


@pytest.mark.asyncio
async def test_tool_registration_count_remains_unchanged() -> None:
    from aem_mcp.server import mcp
    tools = await mcp.list_tools()
    assert len(tools) == 39
    assert len({tool.name for tool in tools}) == 39


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["publish_page", "unpublish_page", "publish_asset", "unpublish_asset", "upload_asset"])
async def test_unproven_direct_operations_fail_closed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from aem_mcp import server
    monkeypatch.setattr(server, "get_settings", direct)
    arguments: dict[str, Any] = {
        "publish_page": {"page_path": "/content/test"},
        "unpublish_page": {"page_path": "/content/test"},
        "publish_asset": {"asset_path": "/content/dam/test.png"},
        "unpublish_asset": {"asset_path": "/content/dam/test.png"},
        "upload_asset": {"dam_folder": "/content/dam", "file_name": "test.png", "content_base64": ""},
    }[name]
    result = await getattr(server, name)(**arguments)
    assert result["error"] == "unsupported_in_cloud_direct_mode"
