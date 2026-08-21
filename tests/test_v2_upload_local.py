from __future__ import annotations

from typing import Any

import httpx
import pytest

from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.services.assets import AssetService, LocalAssetUploadStrategy


def client_with_transport(handler: Any) -> AEMClient:
    instance = AEMClient()
    instance.settings = Settings(
        _env_file=None,
        aem_allowed_roots="/content",
        aem_write_enabled=True,
        aem_write_roots="/content/dam/sigma",
        aem_dam_read_roots="/content/dam",
        aem_dam_write_roots="/content/dam/sigma",
    )
    transport = httpx.MockTransport(handler)
    instance._client = lambda: httpx.AsyncClient(transport=transport, base_url="http://aem")  # type: ignore[method-assign]

    async def no_csrf(_: httpx.AsyncClient) -> dict[str, str]:
        return {}

    instance._csrf_headers = no_csrf  # type: ignore[method-assign]
    return instance


@pytest.mark.asyncio
async def test_local_asset_create_posts_raw_binary_to_full_asset_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            method=request.method,
            path=request.url.path,
            content_type=request.headers.get("content-type"),
            body=request.content,
        )
        return httpx.Response(201, headers={"location": "/api/assets/sigma/koala.jpg"})

    result = await LocalAssetUploadStrategy().upload(
        client_with_transport(handler),
        "/content/dam/sigma",
        "koala.jpg",
        b"image-bytes",
        "image/jpeg",
    )
    assert seen == {
        "method": "POST",
        "path": "/api/assets/sigma/koala.jpg",
        "content_type": "image/jpeg",
        "body": b"image-bytes",
    }
    assert result["statusCode"] == 201 and result["method"] == "POST"


@pytest.mark.asyncio
async def test_local_asset_overwrite_puts_raw_binary_to_full_asset_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, body=request.content)
        return httpx.Response(200)

    result = await LocalAssetUploadStrategy().upload(
        client_with_transport(handler),
        "/content/dam/sigma",
        "koala.jpg",
        b"replacement",
        "image/jpeg",
        overwrite=True,
    )
    assert seen == {
        "method": "PUT",
        "path": "/api/assets/sigma/koala.jpg",
        "body": b"replacement",
    }
    assert result["statusCode"] == 200 and result["method"] == "PUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [412, 500])
async def test_local_asset_upload_rejects_aem_failures_without_response_body(status: int) -> None:
    client = client_with_transport(
        lambda _: httpx.Response(status, text="Authorization: secret server detail")
    )
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        await LocalAssetUploadStrategy().upload(
            client, "/content/dam/sigma", "a.jpg", b"a", "image/jpeg"
        )
    assert "secret" not in str(error.value)
    assert "Authorization" not in str(error.value)


@pytest.mark.asyncio
async def test_local_asset_upload_maps_conflict_to_existing_asset() -> None:
    client = client_with_transport(lambda _: httpx.Response(409))
    with pytest.raises(FileExistsError, match="already exists"):
        await LocalAssetUploadStrategy().upload(
            client, "/content/dam/sigma", "a.jpg", b"a", "image/jpeg"
        )


@pytest.mark.asyncio
async def test_service_chooses_create_or_overwrite_from_existing_state(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = client_with_transport(lambda _: httpx.Response(500))
    calls: list[bool] = []

    class Strategy:
        async def upload(self, *args: Any, overwrite: bool = False) -> dict[str, Any]:
            calls.append(overwrite)
            return {"statusCode": 200 if overwrite else 201}

    service = AssetService(instance, Strategy())
    encoded = "YQ=="
    monkeypatch.setattr(instance, "node_exists", lambda _: async_value(False))
    await service.upload(
        "/content/dam/sigma", "a.jpg", encoded, "image/jpeg", dry_run=False, confirm=True
    )
    monkeypatch.setattr(instance, "node_exists", lambda _: async_value(True))
    await service.upload(
        "/content/dam/sigma", "a.jpg", encoded, "image/jpeg", overwrite=True, dry_run=False, confirm=True
    )
    assert calls == [False, True]


async def async_value(value: Any) -> Any:
    return value
