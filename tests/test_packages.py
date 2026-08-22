from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.services.packages import PackageManagerService


def service(handler: Any | None = None, **overrides: Any) -> tuple[PackageManagerService, list[httpx.Request]]:
    values: dict[str, Any] = {
        "aem_base_url": "http://aem.test",
        "aem_write_enabled": True,
        "aem_write_roots": "/content,/conf",
        "aem_package_allowed_roots": "/content,/content/dam,/conf",
        "aem_verify_ssl": False,
    }
    values.update(overrides)
    client = AEMClient()
    client.settings = Settings(_env_file=None, **values)
    requests: list[httpx.Request] = []
    if handler is not None:
        def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)
        transport = httpx.MockTransport(recording)
        client._client = lambda: httpx.AsyncClient(base_url="http://aem.test", transport=transport)  # type: ignore[method-assign]
        client._csrf_headers = _no_csrf  # type: ignore[method-assign]
    return PackageManagerService(client), requests


async def _no_csrf(_: Any) -> dict[str, str]:
    return {}


def success_handler(request: httpx.Request) -> httpx.Response:
    command = request.url.params.get("cmd")
    message = "Package built" if command == "build" else "Package created" if command == "create" else "Package updated"
    return httpx.Response(200, json={"success": True, "msg": message})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/content/site", "/content/dam/assets"])
async def test_valid_content_dry_run(path: str) -> None:
    package_service, _ = service()
    result = await package_service.create(path, "backup")
    assert result["filterRoot"] == path
    assert result["dryRun"] is True
    assert result["wouldBuild"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/etc", "/system", "/apps", "/libs", "/home", "/var"])
async def test_rejected_sensitive_roots(path: str) -> None:
    package_service, _ = service()
    with pytest.raises((ValueError, PermissionError), match="outside|root"):
        await package_service.create(path, "backup")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/content/../etc", "/content/%2e%2e/etc", "/content/site?x=1", "/content/site#x"])
async def test_rejected_traversal_and_url_parts(path: str) -> None:
    package_service, _ = service()
    with pytest.raises(ValueError, match="traversal|query string"):
        await package_service.create(path, "backup")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("package_name", "bad/name"), ("group", "../group"), ("version", "1/2")],
)
async def test_invalid_package_identifiers(field: str, value: str) -> None:
    package_service, _ = service()
    kwargs = {"package_name": "backup", "group": "mcp", "version": "1.0.0", field: value}
    with pytest.raises(ValueError):
        await package_service.create("/content/site", **kwargs)


@pytest.mark.asyncio
async def test_dry_run_makes_no_mutation_requests() -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be called")
    package_service, requests = service(forbidden)
    result = await package_service.create("/content/site", "backup", dry_run=True)
    assert result["status"] == "dry_run"
    assert requests == []


@pytest.mark.asyncio
async def test_write_disabled_blocks_execution() -> None:
    package_service, _ = service(aem_write_enabled=False)
    with pytest.raises(PermissionError, match="writes are disabled"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
async def test_confirm_false_blocks_execution_without_http() -> None:
    package_service, requests = service(success_handler)
    with pytest.raises(PermissionError, match="confirm=true"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=False)
    assert requests == []


@pytest.mark.asyncio
async def test_create_filter_and_build_success() -> None:
    package_service, requests = service(success_handler)
    result = await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert result == {
        "packageName": "backup", "group": "mcp", "version": "1.0.0",
        "filterRoot": "/content/site", "packagePath": "/etc/packages/mcp/backup-1.0.0.zip",
        "built": True, "status": "Package built",
        "downloadPath": "/etc/packages/mcp/backup-1.0.0.zip", "dryRun": False,
    }
    assert [request.url.params.get("cmd") for request in requests] == ["create", None, "build"]
    filter_data = dict(httpx.QueryParams(requests[1].content.decode()))
    assert json.loads(filter_data["filter"]) == [{"root": "/content/site", "rules": []}]


@pytest.mark.asyncio
async def test_package_already_exists() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "msg": "Package already exists"})
    package_service, _ = service(handler)
    with pytest.raises(FileExistsError, match="already exists"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
async def test_filter_update_failure_stops_before_build() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("update.jsp"):
            return httpx.Response(200, json={"success": False, "msg": "filter rejected"})
        return httpx.Response(200, json={"success": True, "msg": "created"})
    package_service, requests = service(handler)
    with pytest.raises(RuntimeError, match="filter update failed"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_build_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cmd") == "build":
            return httpx.Response(200, json={"success": False, "msg": "build failed"})
        return httpx.Response(200, json={"success": True, "msg": "ok"})
    package_service, _ = service(handler)
    with pytest.raises(RuntimeError, match="build failed"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
async def test_create_without_build() -> None:
    package_service, requests = service(success_handler)
    result = await package_service.create("/content/site", "backup", build=False, dry_run=False, confirm=True)
    assert result["built"] is False
    assert result["status"] == "created"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_malformed_package_manager_response() -> None:
    package_service, _ = service(lambda _: httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"}))
    with pytest.raises(RuntimeError, match="malformed JSON"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_package_manager_auth_errors(status: int) -> None:
    package_service, _ = service(lambda _: httpx.Response(status, text="denied"))
    with pytest.raises(PermissionError, match=f"HTTP {status}"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
async def test_package_manager_500() -> None:
    package_service, _ = service(lambda _: httpx.Response(500, text="failure"))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
async def test_package_audit_events(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="aem_mcp.audit")
    package_service, _ = service(success_handler)
    await package_service.create("/content/site", "backup", dry_run=True)
    await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    events = {json.loads(record.message)["event"] for record in caplog.records}
    assert {
        "aem_package_dry_run", "aem_package_create_attempt", "aem_package_created",
        "aem_package_filter_configured", "aem_package_build_attempt", "aem_package_build_success",
    } <= events
