from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

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
        normalized = self.client._validate_roots(
            path,
            self.settings.package_allowed_roots,
            "AEM_PACKAGE_ALLOWED_ROOTS",
        )
        if normalized == "/":
            raise PermissionError("The repository root may not be packaged")
        if execution:
            self.client._validate_write_path(normalized)
        return normalized

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
            lowered = message.lower()
            if operation == "create" and ("exist" in lowered or "already" in lowered):
                raise FileExistsError("AEM package already exists")
            raise RuntimeError(f"AEM Package Manager {operation} failed: {message[:300]}")
        return payload

    async def create(
        self,
        path: str,
        package_name: str,
        group: str = "mcp",
        version: str = "1.0.0",
        build: bool = True,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        package_name = self._validate_part(package_name, "package_name")
        group = self._validate_part(group, "group")
        version = self._validate_part(version, "version")
        filter_root = self._validate_path(path, execution=not dry_run)
        package_path = f"/etc/packages/{group}/{package_name}-{version}.zip"
        result = {
            "packageName": package_name,
            "group": group,
            "version": version,
            "filterRoot": filter_root,
            "packagePath": package_path,
            "built": False,
            "status": "dry_run" if dry_run else "pending",
            "downloadPath": package_path if build else None,
            "dryRun": dry_run,
        }
        if dry_run:
            result["wouldBuild"] = build
            result["operation"] = "create_configure_filter_and_build" if build else "create_and_configure_filter"
            audit_package_event(self.settings, "aem_package_dry_run", package_name=package_name, group=group, version=version, filter_root=filter_root, build=build)
            return result
        if not confirm:
            raise PermissionError("Package creation refused: call with confirm=true")

        encoded_package_path = quote(package_path, safe="/")
        service_path = f"/crx/packmgr/service/.json{encoded_package_path}"
        async with self.client._client() as http:
            headers = await self.client._csrf_headers(http)
            audit_package_event(self.settings, "aem_package_create_attempt", package_name=package_name, group=group, version=version, filter_root=filter_root)
            create_response = await http.post(
                service_path,
                params={"cmd": "create"},
                data={"packageName": package_name, "groupName": group, "version": version},
                headers=headers,
            )
            self._payload(create_response, "create")
            audit_package_event(self.settings, "aem_package_created", package_name=package_name, group=group, version=version, package_path=package_path)

            filter_response = await http.post(
                "/crx/packmgr/update.jsp",
                data={
                    "path": package_path,
                    "packageName": package_name,
                    "groupName": group,
                    "version": version,
                    "filter": json.dumps([{"root": filter_root, "rules": []}], separators=(",", ":")),
                    "_charset_": "UTF-8",
                },
                headers=headers,
            )
            self._payload(filter_response, "filter update")
            audit_package_event(self.settings, "aem_package_filter_configured", package_name=package_name, package_path=package_path, filter_root=filter_root)

            if build:
                audit_package_event(self.settings, "aem_package_build_attempt", package_name=package_name, package_path=package_path)
                try:
                    build_response = await http.post(service_path, params={"cmd": "build"}, headers=headers)
                    build_payload = self._payload(build_response, "build")
                except Exception as exc:
                    audit_package_event(self.settings, "aem_package_build_failure", package_name=package_name, package_path=package_path, error_code=type(exc).__name__)
                    raise
                audit_package_event(self.settings, "aem_package_build_success", package_name=package_name, package_path=package_path)
                result["built"] = True
                result["status"] = str(build_payload.get("msg") or "built")
            else:
                result["status"] = "created"
        result["dryRun"] = False
        return result
