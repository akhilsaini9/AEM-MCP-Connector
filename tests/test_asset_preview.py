from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from mcp.types import CallToolResult, EmbeddedResource, ImageContent

from aem_mcp.aem_client import AEMClient, BinaryResponse, BinaryTooLargeError
from aem_mcp.services.asset_preview import AssetPreviewService
from aem_mcp.services.assets import AssetService


class FakePreviewClient:
    def __init__(self, mime_type="image/jpeg", renditions=None, binaries=None, roots=("/content/dam",)):
        self.settings = SimpleNamespace(
            aem_max_preview_bytes=1024,
            preview_allowed_mime_types=("image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"),
            mcp_audit_log_enabled=True,
            mcp_audit_log_level="INFO",
        )
        self._mime_type = mime_type
        self._renditions = renditions or {}
        self._binaries = binaries or {}
        self._roots = roots
        self.requests = []

    def validate_dam_read_path(self, path):
        normalized = AEMClient._normalize_path(self, path)
        if not normalized.startswith("/content/dam/") and normalized != "/content/dam":
            raise ValueError("outside DAM")
        if not any(normalized == root or normalized.startswith(root + "/") for root in self._roots):
            raise ValueError("outside AEM_DAM_READ_ROOTS")
        return normalized

    async def _get_node_json(self, path):
        if path.endswith("/jcr:content/renditions"):
            return self._renditions
        raise ValueError(f"AEM node not found: {path}")

    async def get_binary(self, path, max_bytes):
        self.requests.append((path, max_bytes))
        value = self._binaries.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ValueError(f"AEM binary not found: {path}")
        if value.content_length > max_bytes:
            raise BinaryTooLargeError("too large")
        return value


def metadata(path="/content/dam/site/photo.jpg", mime="image/jpeg"):
    return {
        "path": path, "name": path.rsplit("/", 1)[-1], "mime_type": mime,
        "width": 1200, "height": 800, "metadata": {}, "renditions": [],
    }


@pytest.fixture(autouse=True)
def stub_metadata(monkeypatch):
    async def fake_metadata(service, path):
        return metadata(path, service.client._mime_type)
    monkeypatch.setattr(AssetService, "metadata", fake_metadata)


@pytest.mark.parametrize("path", [
    "/content/dam/site/photo.jpg", "/content/dam/site/manual.pdf",
])
def test_preview_path_accepts_dam_assets(path):
    assert FakePreviewClient().validate_dam_read_path(path) == path


@pytest.mark.parametrize("path", [
    "/content/site/photo.jpg", "/apps/x", "/etc/x", "https://example/x",
    "/content/dam/../etc/x", "/content/dam/%2e%2e/x", "/content/dam//x", r"/content/dam\x",
])
def test_preview_path_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        FakePreviewClient().validate_dam_read_path(path)


def test_preview_enforces_dam_read_roots():
    with pytest.raises(ValueError, match="AEM_DAM_READ_ROOTS"):
        FakePreviewClient(roots=("/content/dam/allowed",)).validate_dam_read_path("/content/dam/other/a.jpg")


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "image/webp", "image/gif"])
async def test_native_image_content_for_allowed_mime(mime):
    path = "/content/dam/site/photo.jpg"
    original = path + "/jcr:content/renditions/original"
    client = FakePreviewClient(mime, binaries={original: BinaryResponse(b"image-bytes", mime, 11)})
    result = await AssetPreviewService(client).preview(path)
    assert isinstance(result, CallToolResult)
    image = next(block for block in result.content if isinstance(block, ImageContent))
    assert image.mime_type == mime
    assert base64.b64decode(image.data) == b"image-bytes"
    assert "image_base64" not in result.structured_content


@pytest.mark.asyncio
async def test_html_and_unsupported_asset_mime_rejected():
    for mime in ("text/html", "image/svg+xml", "application/octet-stream"):
        with pytest.raises(ValueError, match="not previewable"):
            await AssetPreviewService(FakePreviewClient(mime)).preview("/content/dam/site/x")


@pytest.mark.asyncio
async def test_response_mime_is_verified_against_metadata():
    path = "/content/dam/site/photo.jpg"
    original = path + "/jcr:content/renditions/original"
    client = FakePreviewClient("image/jpeg", binaries={original: BinaryResponse(b"<html>", "text/html", 6)})
    with pytest.raises(ValueError, match="No image rendition"):
        await AssetPreviewService(client).preview(path)


@pytest.mark.asyncio
async def test_requested_rendition_and_missing_rendition():
    path = "/content/dam/site/photo.jpg"
    rp = path + "/jcr:content/renditions/custom.png"
    client = FakePreviewClient("image/jpeg", {"custom.png": {"jcr:content": {"jcr:mimeType": "image/png"}}}, {rp: BinaryResponse(b"png", "image/png", 3)})
    result = await AssetPreviewService(client).preview(path, "custom.png")
    assert result.structured_content["rendition_name"] == "custom.png"
    with pytest.raises(ValueError, match="Requested rendition not found"):
        await AssetPreviewService(client).preview(path, "missing.png")


@pytest.mark.asyncio
async def test_web_then_thumbnail_then_original_selection_and_size_fallback():
    path = "/content/dam/site/photo.jpg"
    root = path + "/jcr:content/renditions/"
    renditions = {"cq5dam.thumbnail.319.319.png": {}, "cq5dam.web.1280.1280.jpeg": {}, "original": {}}
    binaries = {
        root + "cq5dam.web.1280.1280.jpeg": BinaryTooLargeError("large"),
        root + "cq5dam.thumbnail.319.319.png": BinaryResponse(b"thumb", "image/png", 5),
    }
    result = await AssetPreviewService(FakePreviewClient("image/jpeg", renditions, binaries)).preview(path)
    assert result.structured_content["rendition_name"] == "cq5dam.thumbnail.319.319.png"

    original = root + "original"
    fallback = await AssetPreviewService(FakePreviewClient("image/jpeg", {}, {original: BinaryResponse(b"jpg", "image/jpeg", 3)})).preview(path)
    assert fallback.structured_content["rendition_name"] == "original"
    assert fallback.structured_content["warnings"]


@pytest.mark.asyncio
async def test_no_safe_rendition_is_bounded_error():
    path = "/content/dam/site/photo.jpg"
    original = path + "/jcr:content/renditions/original"
    with pytest.raises(BinaryTooLargeError, match="No image rendition"):
        await AssetPreviewService(FakePreviewClient("image/jpeg", {}, {original: BinaryTooLargeError("large")})).preview(path)


@pytest.mark.asyncio
async def test_pdf_uses_embedded_blob_and_optional_thumbnail():
    path = "/content/dam/site/manual.pdf"
    root = path + "/jcr:content/renditions/"
    client = FakePreviewClient(
        "application/pdf",
        {"original": {"jcr:content": {"jcr:mimeType": "application/pdf"}}, "cq5dam.thumbnail.png": {}},
        {root + "original": BinaryResponse(b"%PDF", "application/pdf", 4), root + "cq5dam.thumbnail.png": BinaryResponse(b"png", "image/png", 3)},
    )
    result = await AssetPreviewService(client).preview(path)
    resource = next(block for block in result.content if isinstance(block, EmbeddedResource))
    assert resource.resource.mime_type == "application/pdf"
    assert base64.b64decode(resource.resource.blob) == b"%PDF"
    assert "admin" not in str(resource.resource.uri)
    assert result.structured_content["thumbnail_available"] is True
    assert any(isinstance(block, ImageContent) for block in result.content)


@pytest.mark.asyncio
async def test_pdf_without_thumbnail_and_oversized_pdf():
    path = "/content/dam/site/manual.pdf"
    original = path + "/jcr:content/renditions/original"
    result = await AssetPreviewService(FakePreviewClient("application/pdf", {}, {original: BinaryResponse(b"%PDF", "application/pdf", 4)})).preview(path)
    assert result.structured_content["thumbnail_available"] is False
    with pytest.raises(BinaryTooLargeError):
        await AssetPreviewService(FakePreviewClient("application/pdf", {}, {original: BinaryTooLargeError("large")})).preview(path)


@pytest.mark.asyncio
async def test_max_bytes_cannot_exceed_server_cap():
    path = "/content/dam/site/photo.jpg"
    original = path + "/jcr:content/renditions/original"
    client = FakePreviewClient("image/jpeg", {}, {original: BinaryResponse(b"x", "image/jpeg", 1)})
    await AssetPreviewService(client).preview(path, max_bytes=999999)
    assert client.requests[0][1] == 1024


@pytest.mark.asyncio
async def test_preview_audit_has_metadata_not_binary(caplog):
    path = "/content/dam/site/photo.jpg"
    original = path + "/jcr:content/renditions/original"
    secret_bytes = b"UNIQUE-BINARY-CONTENT"
    client = FakePreviewClient("image/jpeg", {}, {original: BinaryResponse(secret_bytes, "image/jpeg", len(secret_bytes))})
    with caplog.at_level(logging.INFO, logger="aem_mcp.audit"):
        await AssetPreviewService(client).preview(path)
    record = json.loads(caplog.records[-1].message)
    assert record["event"] == "aem_read_audit"
    assert record["asset_path"] == path
    assert record["byte_count"] == len(secret_bytes)
    assert "UNIQUE-BINARY-CONTENT" not in caplog.text
    assert "Authorization" not in caplog.text


class StreamingClient(AEMClient):
    def __init__(self, handler):
        self.settings = SimpleNamespace(
            aem_base_url="http://aem.test", aem_username="user", aem_password="password",
            aem_timeout_seconds=1, aem_verify_ssl=False,
        )
        self.handler = handler

    def _client(self):
        return httpx.AsyncClient(base_url="http://aem.test", transport=httpx.MockTransport(self.handler))


@pytest.mark.asyncio
async def test_binary_content_length_rejected_before_body_read():
    async def unreadable_stream():
        raise AssertionError("body should not be read")
        yield b"x"

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            async for item in unreadable_stream():
                yield item

    client = StreamingClient(lambda request: httpx.Response(200, headers={"content-length": "100", "content-type": "image/jpeg"}, stream=Stream()))
    with pytest.raises(BinaryTooLargeError):
        await client.get_binary("/content/dam/x.jpg", 10)


@pytest.mark.asyncio
async def test_binary_stream_without_length_stops_at_limit():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123456"
            yield b"789012"
    client = StreamingClient(lambda request: httpx.Response(200, headers={"content-type": "image/jpeg"}, stream=Stream()))
    with pytest.raises(BinaryTooLargeError):
        await client.get_binary("/content/dam/x.jpg", 10)


@pytest.mark.asyncio
async def test_binary_timeout_is_sanitized():
    def timeout(request):
        raise httpx.ReadTimeout("request timed out at http://user:password@aem.test", request=request)
    client = StreamingClient(timeout)
    with pytest.raises(RuntimeError, match="AEM preview request timed out") as exc:
        await client.get_binary("/content/dam/x.jpg", 10)
    assert "password" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(404, ValueError), (401, PermissionError), (403, PermissionError), (500, RuntimeError)])
async def test_binary_aem_errors_are_sanitized(status, error):
    client = StreamingClient(lambda request: httpx.Response(status, content=b"<html>secret error</html>"))
    with pytest.raises(error) as exc:
        await client.get_binary("/content/dam/x.jpg", 100)
    assert "secret error" not in str(exc.value)
