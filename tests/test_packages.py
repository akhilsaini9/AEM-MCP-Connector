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


class RepositoryHandler:
    def __init__(self, *, existing: str | None = None, unrelated: bool = False, built: bool = False, legacy_create: bool = False, bad_filter_readback: bool = False, build_failure: bool = False) -> None:
        self.packages: dict[str, dict[str, Any]] = {}
        self.legacy_create = legacy_create
        self.bad_filter_readback = bad_filter_readback
        self.build_failure = build_failure
        self.build_saw_verified_filter = False
        if existing:
            self.packages[existing] = {"name": "other" if unrelated else "backup", "group": "mcp", "version": "1.0.0" if not existing.endswith("/backup.zip") else "", "built": built, "filter": None}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            raw = path.removesuffix(".infinity.json")
            for package_path, state in self.packages.items():
                definition = f"{package_path}/jcr:content/vlt:definition"
                if raw == definition:
                    return httpx.Response(200, json={"name": state["name"], "group": state["group"], "version": state["version"]})
                if raw == f"{package_path}/jcr:content":
                    return httpx.Response(200, json={":jcr:data": 100 if state["built"] else 0})
                if raw == f"{definition}/filter" and state["filter"] is not None:
                    root = "/content/wrong" if self.bad_filter_readback else state["filter"]
                    return httpx.Response(200, json={"jcr:primaryType": "nt:unstructured", "f0": {"jcr:primaryType": "nt:unstructured", "root": root}})
            return httpx.Response(404)

        data = dict(httpx.QueryParams(request.content.decode())) if request.content else {}
        command = request.url.params.get("cmd")
        if command == "create":
            expected = "/etc/packages/mcp/backup-1.0.0.zip"
            actual = "/etc/packages/mcp/backup.zip" if self.legacy_create else expected
            self.packages[actual] = {"name": "backup", "group": "mcp", "version": "" if self.legacy_create else "1.0.0", "built": False, "filter": None}
            return httpx.Response(200, json={"success": True, "msg": "Package created"})
        if command == "build":
            state = self.packages["/etc/packages/mcp/backup-1.0.0.zip"]
            self.build_saw_verified_filter = state["filter"] == "/content/site" and not self.bad_filter_readback
            if self.build_failure:
                return httpx.Response(200, json={"success": False, "msg": "build failed"})
            state["built"] = True
            return httpx.Response(200, json={"success": True, "msg": "Package built"})
        if data.get(":operation") == "move":
            source = path
            self.packages[data[":dest"]] = self.packages.pop(source)
            return httpx.Response(200, text="moved")
        for package_path, state in self.packages.items():
            definition = f"{package_path}/jcr:content/vlt:definition"
            if path == definition:
                state.update(name=data["name"], group=data["group"], version=data["version"])
            elif path == f"{definition}/filter" and data.get(":operation") == "delete":
                state["filter"] = None
            elif path == f"{definition}/filter/f0":
                state["filter"] = data["root"]
        return httpx.Response(200, text="persisted")


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
    package_service, requests = service(RepositoryHandler())
    with pytest.raises(PermissionError, match="confirm=true"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=False)
    assert requests == []


@pytest.mark.asyncio
async def test_create_filter_and_build_success() -> None:
    handler = RepositoryHandler()
    package_service, requests = service(handler)
    result = await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert result == {
        "packageName": "backup", "group": "mcp", "version": "1.0.0",
        "filterRoot": "/content/site", "packagePath": "/etc/packages/mcp/backup-1.0.0.zip",
        "built": True, "status": "Package built",
        "downloadPath": "/etc/packages/mcp/backup-1.0.0.zip",
        "downloadUrl": "http://aem.test/etc/packages/mcp/backup-1.0.0.zip", "dryRun": False,
    }
    assert handler.packages[result["packagePath"]]["filter"] == "/content/site"
    assert handler.build_saw_verified_filter is True
    assert not any(request.url.path.endswith("update.jsp") for request in requests)


def test_download_url_never_contains_configured_userinfo() -> None:
    package_service, _ = service(aem_base_url="http://user:secret@aem.test:4502")
    assert package_service._download_url("/etc/packages/mcp/backup.zip") == "http://aem.test:4502/etc/packages/mcp/backup.zip"


@pytest.mark.asyncio
async def test_update_jsp_404_is_irrelevant_when_repository_update_succeeds() -> None:
    handler = RepositoryHandler()
    package_service, requests = service(handler)
    result = await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert result["built"] is True
    assert all(request.url.path != "/crx/packmgr/update.jsp" for request in requests)


@pytest.mark.asyncio
async def test_existing_half_created_package_resumes_safely() -> None:
    expected = "/etc/packages/mcp/backup-1.0.0.zip"
    handler = RepositoryHandler(existing=expected)
    package_service, requests = service(handler)
    result = await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert result["built"] is True
    assert handler.packages[expected]["filter"] == "/content/site"
    assert not any(request.url.params.get("cmd") == "create" for request in requests)


@pytest.mark.asyncio
async def test_existing_legacy_half_created_package_is_normalized_and_resumed() -> None:
    legacy = "/etc/packages/mcp/backup.zip"
    expected = "/etc/packages/mcp/backup-1.0.0.zip"
    handler = RepositoryHandler(existing=legacy)
    package_service, requests = service(handler)
    result = await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert result["packagePath"] == expected
    assert legacy not in handler.packages
    assert handler.packages[expected]["version"] == "1.0.0"
    assert any(dict(httpx.QueryParams(request.content.decode())).get(":operation") == "move" for request in requests if request.content)


@pytest.mark.asyncio
async def test_package_already_exists() -> None:
    handler = RepositoryHandler(existing="/etc/packages/mcp/backup-1.0.0.zip", unrelated=True)
    package_service, _ = service(handler)
    with pytest.raises(FileExistsError, match="unrelated"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
async def test_filter_update_failure_stops_before_build() -> None:
    handler = RepositoryHandler(bad_filter_readback=True)
    package_service, requests = service(handler)
    with pytest.raises(RuntimeError, match="read-back"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert not any(request.url.params.get("cmd") == "build" for request in requests)


@pytest.mark.asyncio
async def test_repository_filter_update_transport_failure() -> None:
    repository = RepositoryHandler()
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/vlt:definition/filter/f0"):
            return httpx.Response(500, text="failed")
        return repository(request)
    package_service, requests = service(handler)
    with pytest.raises(RuntimeError, match="filter root creation failed: HTTP 500"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    assert not any(request.url.params.get("cmd") == "build" for request in requests)


@pytest.mark.asyncio
async def test_build_failure() -> None:
    handler = RepositoryHandler(build_failure=True)
    package_service, _ = service(handler)
    with pytest.raises(RuntimeError, match="build failed"):
        await package_service.create("/content/site", "backup", dry_run=False, confirm=True)


@pytest.mark.asyncio
async def test_create_without_build() -> None:
    package_service, requests = service(RepositoryHandler())
    result = await package_service.create("/content/site", "backup", build=False, dry_run=False, confirm=True)
    assert result["built"] is False
    assert result["status"] == "created"
    assert not any(request.url.params.get("cmd") == "build" for request in requests)


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
    package_service, _ = service(RepositoryHandler())
    await package_service.create("/content/site", "backup", dry_run=True)
    await package_service.create("/content/site", "backup", dry_run=False, confirm=True)
    events = {json.loads(record.message)["event"] for record in caplog.records}
    assert {
        "aem_package_dry_run", "aem_package_create_attempt", "aem_package_created",
        "aem_package_filter_configured", "aem_package_build_attempt", "aem_package_build_success",
    } <= events
