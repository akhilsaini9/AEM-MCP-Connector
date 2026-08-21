from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
import pytest

from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.services.assets import (
    AssetService,
    LocalAssetUploadStrategy,
    dam_path_to_assets_api_path,
)
from aem_mcp.services.publication import LocalReplicationStrategy, PublicationService


def configured_client(**overrides: Any) -> AEMClient:
    values: dict[str, Any] = {
        "aem_allowed_roots": "/content",
        "aem_write_enabled": True,
        "aem_write_roots": "/content/dam/site",
        "aem_dam_read_roots": "/content/dam",
        "aem_dam_write_roots": "/content/dam/site",
        "aem_publish_allowed_roots": "/content/dam/site",
    }
    values.update(overrides)
    client = AEMClient()
    client.settings = Settings(_env_file=None, **values)
    return client


class RecordingReplication:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def replicate(self, *args: Any) -> dict[str, Any]:
        self.calls.append(args)
        return {"success": True}


@pytest.mark.asyncio
async def test_asset_publication_requires_all_root_controls() -> None:
    allowed = configured_client()
    strategy = RecordingReplication()
    result = await PublicationService(allowed, strategy).asset(
        "/content/dam/site/a.jpg", "Activate", dry_run=False, confirm=True
    )
    assert result["success"] and len(strategy.calls) == 1

    publish_denied = configured_client(aem_publish_allowed_roots="/content/dam/other")
    with pytest.raises(PermissionError, match="PUBLISH"):
        await PublicationService(publish_denied, RecordingReplication()).asset(
            "/content/dam/site/a.jpg", "Activate", dry_run=False, confirm=True
        )

    dam_denied = configured_client(
        aem_write_roots="/content/dam",
        aem_dam_write_roots="/content/dam/other",
        aem_publish_allowed_roots="/content/dam/site",
    )
    with pytest.raises(PermissionError, match="DAM_WRITE"):
        await PublicationService(dam_denied, RecordingReplication()).asset(
            "/content/dam/site/a.jpg", "Deactivate", dry_run=False, confirm=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/content/dam/site-other/a.jpg",
        "/content/dam/site/../other/a.jpg",
        "/content/dam/site/./a.jpg",
        "/content/dam/site//a.jpg",
        "/content/dam/site/%2e%2e/other/a.jpg",
        "/content/dam/site%2fother/a.jpg",
    ],
)
async def test_asset_publication_rejects_prefix_and_traversal(path: str) -> None:
    strategy = RecordingReplication()
    with pytest.raises((ValueError, PermissionError)):
        await PublicationService(configured_client(), strategy).asset(
            path, "Activate", dry_run=False, confirm=True
        )
    assert strategy.calls == []


@pytest.mark.asyncio
async def test_oversized_base64_rejected_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    client = configured_client(aem_max_asset_upload_bytes=3)
    called = False

    def decode(_: str) -> bytes:
        nonlocal called
        called = True
        return b""

    monkeypatch.setattr(client, "decode_base64_content", decode)
    with pytest.raises(ValueError, match="Encoded asset"):
        await AssetService(client).upload(
            "/content/dam/site", "a.jpg", "A" * 9, "image/jpeg"
        )
    assert not called


@pytest.mark.asyncio
async def test_base64_precheck_and_decoded_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    valid = configured_client(aem_max_asset_upload_bytes=3)
    monkeypatch.setattr(valid, "node_exists", lambda _: async_value(False))
    result = await AssetService(valid).upload(
        "/content/dam/site", "a.jpg", base64.b64encode(b"abc").decode(), "image/jpeg"
    )
    assert result["size"] == 3

    above = configured_client(aem_max_asset_upload_bytes=1)
    monkeypatch.setattr(above, "node_exists", lambda _: async_value(False))
    with pytest.raises(ValueError, match="Asset exceeds"):
        await AssetService(above).upload(
            "/content/dam/site", "a.jpg", base64.b64encode(b"ab").decode(), "image/jpeg"
        )


@pytest.mark.asyncio
async def test_malformed_base64_and_error_logs_do_not_expose_content(caplog: pytest.LogCaptureFixture) -> None:
    supplied = "private-upload-content!!!"
    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError, match="valid base64") as error:
            await AssetService(configured_client()).upload(
                "/content/dam/site", "a.jpg", supplied, "image/jpeg"
            )
    assert supplied not in str(error.value)
    assert supplied not in caplog.text


@pytest.mark.parametrize(
    ("repository_path", "api_path"),
    [
        ("/content/dam", "/api/assets"),
        ("/content/dam/", "/api/assets"),
        ("/content/dam/site", "/api/assets/site"),
        ("/content/dam/site/folder", "/api/assets/site/folder"),
        ("/content/dam/site/my folder", "/api/assets/site/my%20folder"),
    ],
)
def test_dam_path_to_assets_api_path(repository_path: str, api_path: str) -> None:
    assert dam_path_to_assets_api_path(repository_path) == api_path


@pytest.mark.parametrize(
    "path",
    ["", "content/dam", "/content/site", "/content/dam//site", "/content/dam/../site", "/content/dam/%2e%2e/site", "/content/dam/site%2ffolder"],
)
def test_dam_path_to_assets_api_path_rejects_invalid_paths(path: str) -> None:
    with pytest.raises(ValueError):
        dam_path_to_assets_api_path(path)


@pytest.mark.asyncio
async def test_upload_strategy_uses_mapped_assets_api_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client = configured_client()
    calls: list[tuple[Any, ...]] = []

    async def post(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((*args, kwargs))
        return {"statusCode": 201}

    monkeypatch.setattr(client, "put_asset_binary", post)
    await LocalAssetUploadStrategy().upload(
        client, "/content/dam/site/folder", "a.jpg", b"a", "image/jpeg"
    )
    assert calls[0][0] == "/api/assets/site/folder/a.jpg"
    assert calls[0][-1] == {"overwrite": False}


def replication_client(response: httpx.Response, states: list[dict[str, Any]] | None = None) -> AEMClient:
    client = configured_client()
    transport = httpx.MockTransport(lambda _: response)
    client._client = lambda: httpx.AsyncClient(transport=transport, base_url="http://aem")  # type: ignore[method-assign]

    async def no_csrf(_: httpx.AsyncClient) -> dict[str, str]:
        return {}

    client._csrf_headers = no_csrf  # type: ignore[method-assign]
    if states is not None:
        remaining = list(states)
        async def node(_: str) -> dict[str, Any]:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]
        client._get_node_json = node  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="Authorization: secret"),
        httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>"),
        httpx.Response(200, headers={"content-type": "application/json"}, text="not-json"),
    ],
)
async def test_replication_transport_rejects_invalid_responses(response: httpx.Response) -> None:
    with pytest.raises(RuntimeError) as error:
        await LocalReplicationStrategy().replicate(
            replication_client(response, [{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"before"}]), "/content/dam/site/a.jpg", "Activate"
        )
    assert "secret" not in str(error.value)
    assert "Authorization" not in str(error.value)


@pytest.mark.asyncio
async def test_replication_rejects_explicit_failure_and_accepts_explicit_success() -> None:
    failed = httpx.Response(
        200, headers={"content-type": "application/json"}, json={"success": False, "error": "denied"}
    )
    with pytest.raises(RuntimeError, match="reported failure"):
        await LocalReplicationStrategy().replicate(
            replication_client(failed, [{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"before"}]), "/content/dam/site/a.jpg", "Activate"
        )

    succeeded = httpx.Response(
        200, headers={"content-type": "application/json"}, json={"success": True}
    )
    result = await LocalReplicationStrategy().replicate(
        replication_client(succeeded, [{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"before"},{"cq:lastReplicationAction":"Activate","cq:lastReplicated":"after","cq:lastReplicatedBy":"admin"}]), "/content/dam/site/a.jpg", "Activate"
    )
    assert result["request_accepted"] is True
    assert result["author_status_verified"] is True
    assert result["delivery_to_publish_verified"] is False
    assert result["replication_action"] == "Activate"


async def async_value(value: Any) -> Any:
    return value
