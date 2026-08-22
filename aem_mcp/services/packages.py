from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
import httpx
from ..aem_client import AEMClient
from ..audit import audit_package_event

_PACKAGE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class PackageManagerService:
    """Guarded AEM 6.5 CRX Package Manager create/filter/build workflow."""

    def __init__(self, client: AEMClient) -> None:
        self.client = client
        self.settings = client.settings

    @staticmethod
    def _validate_part(value: str, label: str) -> str:
        if not isinstance(value, str) or value != value.strip() or not _PACKAGE_PART.fullmatch(value):
            raise ValueError(f"{label} must contain only letters, numbers, '.', '_' or '-' and may not contain slashes")
        if value in {".", ".."} or ".." in value:
            raise ValueError(f"{label} may not contain traversal")
        return value

    def _validate_path(self, path: str, *, execution: bool) -> str:
        if not isinstance(path, str) or "?" in path or "#" in path:
            raise ValueError("Package filter path may not contain a query string or fragment")
        normalized = self.client._validate_roots(path, self.settings.package_allowed_roots, "AEM_PACKAGE_ALLOWED_ROOTS")
        if normalized == "/":
            raise PermissionError("The repository root may not be packaged")
        if execution:
            self.client._validate_write_path(normalized)
        return normalized

    def _download_url(self, package_path: str) -> str:
        parsed = urlsplit(self.settings.aem_base_url.rstrip("/"))
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, package_path, "", ""))

    @staticmethod
    def _payload(response: httpx.Response, operation: str) -> dict[str, Any]:
        if response.status_code in {401, 403}:
            raise PermissionError(f"AEM Package Manager {operation} was not authorized: HTTP {response.status_code}")
        if response.status_code == 404:
            raise RuntimeError(f"AEM Package Manager {operation} endpoint was not found: HTTP 404")
        if operation == "create" and response.status_code == 409:
            raise FileExistsError("AEM package already exists")
        if response.status_code >= 500:
            raise RuntimeError(f"AEM Package Manager {operation} failed: HTTP {response.status_code}")
        if not response.is_success:
            raise RuntimeError(f"AEM Package Manager {operation} failed: HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"AEM Package Manager {operation} returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"AEM Package Manager {operation} returned an unexpected response")
        if payload.get("success") is not True:
            message = str(payload.get("msg") or payload.get("message") or "operation reported failure")
            if operation == "create" and ("exist" in message.lower() or "already" in message.lower()):
                raise FileExistsError("AEM package already exists")
            raise RuntimeError(f"AEM Package Manager {operation} failed: {message[:300]}")
        return payload

    @staticmethod
    async def _json_node(http: httpx.AsyncClient, path: str) -> dict[str, Any] | None:
        response = await http.get(f"{quote(path, safe='/')}.infinity.json")
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise PermissionError(f"AEM package definition read was not authorized: HTTP {response.status_code}")
        if not response.is_success:
            raise RuntimeError(f"AEM package definition read failed: HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("AEM package definition read returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("AEM package definition read returned an unexpected response")
        return payload

    @staticmethod
    async def _sling_post(http: httpx.AsyncClient, path: str, data: dict[str, Any], headers: dict[str, str], operation: str) -> None:
        response = await http.post(quote(path, safe="/"), data=data, headers=headers)
        if response.status_code in {401, 403}:
            raise PermissionError(f"AEM repository {operation} was not authorized: HTTP {response.status_code}")
        if response.status_code == 404:
            raise RuntimeError(f"AEM repository {operation} target was not found: HTTP 404")
        if not response.is_success:
            raise RuntimeError(f"AEM repository {operation} failed: HTTP {response.status_code}")

    async def _package_state(self, http: httpx.AsyncClient, package_path: str) -> dict[str, Any] | None:
        definition = await self._json_node(http, f"{package_path}/jcr:content/vlt:definition")
        if definition is None:
            return None
        content = await self._json_node(http, f"{package_path}/jcr:content") or {}
        size = content.get(":jcr:data", 0)
        return {"path": package_path, "definition": definition, "built": isinstance(size, (int, float)) and size > 0}

    @staticmethod
    def _identity_matches(state: dict[str, Any], name: str, group: str, version: str, *, legacy: bool = False) -> bool:
        definition = state["definition"]
        actual_version = str(definition.get("version", ""))
        return definition.get("name") == name and definition.get("group") == group and (actual_version == version or (legacy and actual_version == ""))

    async def _locate_or_create(self, http: httpx.AsyncClient, headers: dict[str, str], package_path: str, legacy_path: str, name: str, group: str, version: str, root: str) -> None:
        expected = await self._package_state(http, package_path)
        if expected is not None:
            if not self._identity_matches(expected, name, group, version) or expected["built"]:
                raise FileExistsError("An existing package is unrelated or already built; it will not be overwritten")
            return
        legacy = await self._package_state(http, legacy_path)
        if legacy is not None:
            if not self._identity_matches(legacy, name, group, version, legacy=True) or legacy["built"]:
                raise FileExistsError("An existing legacy package is unrelated or already built; it will not be overwritten")
            await self._sling_post(http, legacy_path, {":operation": "move", ":dest": package_path}, headers, "package resume move")
            if await self._package_state(http, package_path) is None:
                raise RuntimeError("AEM package resume move could not be verified")
            return
        service_path = f"/crx/packmgr/service/.json{quote(package_path, safe='/')}"
        audit_package_event(self.settings, "aem_package_create_attempt", package_name=name, group=group, version=version, filter_root=root)
        response = await http.post(service_path, params={"cmd": "create"}, data={"packageName": name, "groupName": group, "version": version}, headers=headers)
        self._payload(response, "create")
        if await self._package_state(http, package_path) is None:
            legacy = await self._package_state(http, legacy_path)
            if legacy is None or not self._identity_matches(legacy, name, group, version, legacy=True) or legacy["built"]:
                raise RuntimeError("AEM Package Manager create succeeded but the expected package definition was not found")
            await self._sling_post(http, legacy_path, {":operation": "move", ":dest": package_path}, headers, "package normalization move")
            if await self._package_state(http, package_path) is None:
                raise RuntimeError("AEM package normalization move could not be verified")
        audit_package_event(self.settings, "aem_package_created", package_name=name, group=group, version=version, package_path=package_path)

    async def _configure_and_verify_filter(self, http: httpx.AsyncClient, headers: dict[str, str], package_path: str, name: str, group: str, version: str, root: str) -> None:
        definition_path = f"{package_path}/jcr:content/vlt:definition"
        filter_path = f"{definition_path}/filter"
        if await self._json_node(http, filter_path) is not None:
            await self._sling_post(http, filter_path, {":operation": "delete"}, headers, "old filter removal")
            if await self._json_node(http, filter_path) is not None:
                raise RuntimeError("AEM repository filter removal could not be verified")
        await self._sling_post(http, definition_path, {"name": name, "group": group, "version": version}, headers, "package definition update")
        await self._sling_post(http, filter_path, {"jcr:primaryType": "nt:unstructured"}, headers, "filter container creation")
        await self._sling_post(http, f"{filter_path}/f0", {"jcr:primaryType": "nt:unstructured", "root": root}, headers, "filter root creation")
        definition = await self._json_node(http, definition_path)
        persisted = await self._json_node(http, filter_path)
        if definition is None or not self._identity_matches({"definition": definition}, name, group, version):
            raise RuntimeError("AEM package definition identity verification failed")
        if persisted is None:
            raise RuntimeError("AEM package filter read-back verification failed")
        children = [value for value in persisted.values() if isinstance(value, dict)]
        if len(children) != 1:
            raise RuntimeError("AEM package filter verification found an unexpected number of filter roots")
        only = children[0]
        rules = [value for value in only.values() if isinstance(value, dict)]
        if only.get("root") != root or rules:
            raise RuntimeError("AEM package filter read-back did not match the exact requested root")
        audit_package_event(self.settings, "aem_package_filter_configured", package_name=name, package_path=package_path, filter_root=root)

    async def create(self, path: str, package_name: str, group: str = "mcp", version: str = "1.0.0", build: bool = True, dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
        package_name = self._validate_part(package_name, "package_name")
        group = self._validate_part(group, "group")
        version = self._validate_part(version, "version")
        root = self._validate_path(path, execution=not dry_run)
        package_path = f"/etc/packages/{group}/{package_name}-{version}.zip"
        legacy_path = f"/etc/packages/{group}/{package_name}.zip"
        result = {"packageName": package_name, "group": group, "version": version, "filterRoot": root, "packagePath": package_path, "built": False, "status": "dry_run" if dry_run else "pending", "downloadPath": package_path if build else None, "downloadUrl": self._download_url(package_path) if build else None, "dryRun": dry_run}
        if dry_run:
            result.update(wouldBuild=build, operation="create_configure_filter_and_build" if build else "create_and_configure_filter")
            audit_package_event(self.settings, "aem_package_dry_run", package_name=package_name, group=group, version=version, filter_root=root, build=build)
            return result
        if not confirm:
            raise PermissionError("Package creation refused: call with confirm=true")
        async with self.client._client() as http:
            headers = await self.client._csrf_headers(http)
            await self._locate_or_create(http, headers, package_path, legacy_path, package_name, group, version, root)
            await self._configure_and_verify_filter(http, headers, package_path, package_name, group, version, root)
            if build:
                audit_package_event(self.settings, "aem_package_build_attempt", package_name=package_name, package_path=package_path)
                try:
                    response = await http.post(f"/crx/packmgr/service/.json{quote(package_path, safe='/')}", params={"cmd": "build"}, headers=headers)
                    payload = self._payload(response, "build")
                except Exception as exc:
                    audit_package_event(self.settings, "aem_package_build_failure", package_name=package_name, package_path=package_path, error_code=type(exc).__name__)
                    raise
                result["built"] = True
                result["status"] = str(payload.get("msg") or "built")
                audit_package_event(self.settings, "aem_package_build_success", package_name=package_name, package_path=package_path)
            else:
                result["status"] = "created"
        result["dryRun"] = False
        return result
