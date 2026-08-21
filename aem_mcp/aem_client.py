from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
import base64
import re
import httpx

from .config import get_settings


@dataclass(frozen=True)
class BinaryResponse:
    content: bytes
    mime_type: str
    content_length: int


class BinaryTooLargeError(ValueError):
    """Raised when an outbound AEM binary exceeds its bounded read limit."""

class AEMClient:
    """HTTP wrapper around a local AEM Author SDK with guarded CRUD support."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _normalize_path(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("AEM path must start with '/'")
        lowered = path.lower()
        if (
            "/../" in path
            or path.endswith("/..")
            or "/./" in path
            or "//" in path
            or "\\" in path
            or any(token in lowered for token in ("%2e", "%2f", "%5c"))
        ):
            raise ValueError("Path traversal is not allowed")
        return path.rstrip("/") or "/"

    def _validate_read_path(self, path: str) -> str:
        normalized = self._normalize_path(path)
        roots = self.settings.allowed_roots
        if roots and not any(
            normalized == root or normalized.startswith(root + "/")
            for root in roots
        ):
            raise ValueError(
                f"Path '{normalized}' is outside allowed roots: {', '.join(roots)}"
            )
        return normalized

    def _validate_write_path(self, path: str) -> str:
        if not self.settings.aem_write_enabled:
            raise PermissionError(
                "AEM writes are disabled. Set AEM_WRITE_ENABLED=true in .env to enable them."
            )
        normalized = self._normalize_path(path)
        roots = self.settings.write_roots
        if not roots:
            raise PermissionError("AEM_WRITE_ROOTS is empty; no writes are permitted.")
        if not any(
            normalized == root or normalized.startswith(root + "/")
            for root in roots
        ):
            raise PermissionError(
                f"Write path '{normalized}' is outside AEM_WRITE_ROOTS: {', '.join(roots)}"
            )
        return normalized

    def _validate_roots(self, path: str, roots: tuple[str, ...], label: str, *, require_write: bool = False) -> str:
        normalized = self._normalize_path(path)
        if require_write:
            # Domain-specific roots narrow, but never replace, the legacy write gate.
            self._validate_write_path(normalized)
        if not roots or not any(normalized == root or normalized.startswith(root + "/") for root in roots):
            error = PermissionError if require_write else ValueError
            raise error(f"Path '{normalized}' is outside {label}: {', '.join(roots) or '[none]'}")
        return normalized

    def validate_publish_path(self, path: str, *, require_write: bool = False) -> str:
        return self._validate_roots(path, self.settings.publish_allowed_roots, "AEM_PUBLISH_ALLOWED_ROOTS", require_write=require_write)

    def validate_dam_read_path(self, path: str) -> str:
        return self._validate_roots(path, self.settings.dam_read_roots, "AEM_DAM_READ_ROOTS")

    def validate_dam_write_path(self, path: str, *, require_write: bool = True) -> str:
        return self._validate_roots(path, self.settings.dam_write_roots, "AEM_DAM_WRITE_ROOTS", require_write=require_write)

    def _validate_component_path(self, path: str, *, write: bool = False) -> str:
        """Validate a component path and its owning page, without substring tricks."""
        normalized = self._normalize_path(path)
        segments = normalized.split("/")[1:]
        marker_indexes = [
            index for index, segment in enumerate(segments)
            if segment == "jcr:content"
        ]
        if len(marker_indexes) != 1:
            raise ValueError("Component path must be below a page's jcr:content")
        marker_index = marker_indexes[0]
        if marker_index == 0:
            raise ValueError("Component path must identify an owning page")
        page_path = "/" + "/".join(segments[:marker_index])
        if write:
            self._validate_write_path(page_path)
        else:
            self._validate_read_path(page_path)
        return normalized

    @staticmethod
    def _safe_properties(data: dict[str, Any]) -> dict[str, Any]:
        """Return bounded JSON-safe direct properties, excluding child nodes/metadata."""
        excluded = {
            "jcr:primaryType", "jcr:mixinTypes", "jcr:uuid", "jcr:created",
            "jcr:createdBy", "jcr:lastModified", "jcr:lastModifiedBy",
            "cq:lastModified", "cq:lastModifiedBy",
            "sling:resourceType",
        }

        def bounded(value: Any) -> Any:
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if isinstance(value, str):
                return value if len(value) <= 10000 else value[:10000] + "...[truncated]"
            if isinstance(value, list):
                return [bounded(item) for item in value[:100]]
            return None

        result: dict[str, Any] = {}
        for key, value in data.items():
            if key in excluded or key.startswith(":") or isinstance(value, dict):
                continue
            safe = bounded(value)
            if safe is not None:
                result[key] = safe
        return result

    async def _get_node_json(self, node_path: str) -> dict[str, Any]:
        url = f"{quote(node_path, safe='/')}.1.json"
        async with self._client() as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise ValueError(f"AEM node not found: {node_path}")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected Sling JSON response for: {node_path}")
        return payload

    async def node_exists(self, node_path: str) -> bool:
        try:
            await self._get_node_json(node_path)
            return True
        except ValueError as exc:
            if "not found" in str(exc):
                return False
            raise

    async def get_binary(self, path: str, max_bytes: int) -> BinaryResponse:
        """Fetch an authenticated AEM binary without reading beyond max_bytes."""
        normalized = self._normalize_path(path)
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        try:
            async with self._client() as client:
                async with client.stream(
                    "GET", quote(normalized, safe="/"), headers={"Accept": "*/*"}
                ) as response:
                    if response.status_code == 404:
                        raise ValueError(f"AEM binary not found: {normalized}")
                    if response.status_code in {401, 403}:
                        raise PermissionError("AEM authentication or authorization failed while retrieving preview")
                    if not response.is_success:
                        raise RuntimeError(f"AEM binary request failed: HTTP {response.status_code}")
                    raw_length = response.headers.get("content-length")
                    if raw_length:
                        try:
                            declared_length = int(raw_length)
                        except ValueError as exc:
                            raise RuntimeError("AEM binary returned an invalid Content-Length") from exc
                        if declared_length > max_bytes:
                            raise BinaryTooLargeError("AEM preview exceeds AEM_MAX_PREVIEW_BYTES")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise BinaryTooLargeError("AEM preview exceeds AEM_MAX_PREVIEW_BYTES")
                        chunks.append(chunk)
                    mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    return BinaryResponse(b"".join(chunks), mime_type, total)
        except httpx.TimeoutException as exc:
            raise RuntimeError("AEM preview request timed out") from exc

    async def post_form_unchecked(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self._post_form(path, data)

    async def request_replication(self, path: str, action: str) -> dict[str, Any]:
        """Send a Local replication request while preserving response evidence."""
        async with self._client() as client:
            headers = await self._csrf_headers(client)
            response = await client.post(
                "/bin/replicate.json",
                data={"path": path, "cmd": action},
                headers=headers,
            )
            if not response.is_success:
                raise RuntimeError(
                    f"AEM replication transport failed: HTTP {response.status_code}"
                )
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type != "application/json":
                raise RuntimeError(
                    f"Unexpected AEM replication response content type: {content_type or '[missing]'}"
                )
            try:
                payload = response.json()
            except Exception as exc:
                raise RuntimeError("AEM replication returned malformed JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("AEM replication returned an unexpected JSON payload")
            return payload

    async def put_asset_binary(
        self,
        path: str,
        content: bytes,
        mime_type: str,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        """Create or replace an AEM 6.5 asset using its full Assets API path."""
        method = "PUT" if overwrite else "POST"
        expected_status = 200 if overwrite else 201
        async with self._client() as client:
            headers = await self._csrf_headers(client)
            headers["Content-Type"] = mime_type
            response = await client.request(method, path, content=content, headers=headers)
            if response.status_code == 409:
                raise FileExistsError("AEM asset already exists")
            if response.status_code != expected_status:
                raise RuntimeError(
                    f"AEM asset upload failed: HTTP {response.status_code}"
                )
            return {
                "statusCode": response.status_code,
                "location": response.headers.get("Location"),
                "contentType": response.headers.get("content-type"),
                "method": method,
            }

    @staticmethod
    def decode_base64_content(content_base64: str) -> bytes:
        try:
            return base64.b64decode(content_base64, validate=True)
        except Exception as exc:
            raise ValueError("content_base64 must be valid base64") from exc

    async def walk_components(
        self, page_path: str, max_depth: int = 10, limit: int = 200
    ) -> dict[str, Any]:
        page_path = self._validate_read_path(page_path)
        max_depth = min(max(max_depth, 0), 50)
        limit = min(max(limit, 1), 1000)
        content_path = f"{page_path}/jcr:content"
        root = await self._get_node_json(content_path)
        components: list[dict[str, Any]] = []
        incomplete_reasons: set[str] = set()

        async def visit(parent_path: str, tree: dict[str, Any], depth: int) -> None:
            for name, child in tree.items():
                if not isinstance(child, dict):
                    continue
                if len(components) >= limit:
                    incomplete_reasons.add("limit")
                    return
                child_path = f"{parent_path}/{name}"
                resource_type = child.get("sling:resourceType")
                if isinstance(resource_type, str) and resource_type.strip():
                    components.append({
                        "path": child_path,
                        "name": name,
                        "resourceType": resource_type,
                        "properties": self._safe_properties(child),
                    })
                # Sling's .1.json selector guarantees only one child level. Fetch
                # each child separately so traversal is genuinely recursive while
                # every individual response remains bounded.
                child_tree = await self._get_node_json(child_path)
                has_children = any(isinstance(value, dict) for value in child_tree.values())
                if has_children and depth >= max_depth:
                    incomplete_reasons.add("max_depth")
                elif has_children:
                    await visit(child_path, child_tree, depth + 1)
                if len(components) >= limit:
                    # There may be unvisited siblings or descendants.
                    incomplete_reasons.add("limit")
                    return

        await visit(content_path, root, 1)
        return {
            "pagePath": page_path,
            "contentPath": content_path,
            "components": components,
            "count": len(components),
            "maxDepth": max_depth,
            "limit": limit,
            "incomplete": bool(incomplete_reasons),
            "incompleteReasons": sorted(incomplete_reasons),
        }

    async def list_components(
        self, page_path: str, max_depth: int = 10, limit: int = 200
    ) -> dict[str, Any]:
        return await self.walk_components(page_path, max_depth, limit)

    async def get_component_properties(self, component_path: str) -> dict[str, Any]:
        component_path = self._validate_component_path(component_path)
        data = await self._get_node_json(component_path)
        resource_type = data.get("sling:resourceType")
        return {
            "path": component_path,
            "name": component_path.rsplit("/", 1)[-1],
            "resourceType": resource_type,
            "properties": self._safe_properties(data),
        }

    async def find_components(
        self,
        page_path: str,
        resource_type: str | None = None,
        name_contains: str | None = None,
        property_name: str | None = None,
        property_value: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        # Traverse with the caller's result cap; all filtering shares one discovery path.
        discovered = await self.walk_components(page_path, max_depth=10, limit=1000)
        matches: list[dict[str, Any]] = []
        limit = min(max(limit, 1), 1000)
        for component in discovered["components"]:
            props = component["properties"]
            if resource_type is not None and component["resourceType"] != resource_type:
                continue
            if name_contains is not None and name_contains.lower() not in component["name"].lower():
                continue
            if property_name is not None:
                if property_name not in props:
                    continue
                if property_value is not None and props[property_name] != property_value:
                    continue
            matches.append(component)
            if len(matches) >= limit:
                break
        return {
            "pagePath": discovered["pagePath"],
            "components": matches,
            "count": len(matches),
            "limit": limit,
            "incomplete": discovered["incomplete"] or len(matches) >= limit,
            "incompleteReasons": sorted(set(discovered["incompleteReasons"] + (["limit"] if len(matches) >= limit else []))),
        }

    @staticmethod
    def _validate_component_properties(properties: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(properties, dict):
            raise ValueError("properties must be a JSON object")
        protected = {"jcr:primaryType", "sling:resourceType"}
        for name, value in properties.items():
            if not isinstance(name, str) or not name.strip() or "/" in name or name.startswith(":"):
                raise ValueError(f"Invalid component property name: {name!r}")
            if name in protected or name.startswith("jcr:") or name.startswith("sling:"):
                raise ValueError(f"Protected component property is not allowed: {name}")
            if isinstance(value, (dict, bytes, bytearray)):
                raise ValueError(f"Component property must be a scalar or list: {name}")
            if isinstance(value, list) and any(isinstance(item, (dict, list, bytes, bytearray)) for item in value):
                raise ValueError(f"Component property list must contain scalar values: {name}")
        return properties

    async def add_component(
        self,
        parent_path: str,
        node_name: str,
        resource_type: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parent_path = self._validate_component_path(parent_path, write=True)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", node_name or "") or node_name in {".", ".."}:
            raise ValueError("node_name must be one valid JCR node name")
        if not resource_type or resource_type.strip() != resource_type or resource_type.startswith("/") or ".." in resource_type.split("/"):
            raise ValueError("resource_type must be a non-empty relative resource type")
        allowlist = self.settings.component_allowed_resource_types
        if allowlist and resource_type not in allowlist:
            raise PermissionError("resource_type is not allowed by AEM_COMPONENT_ALLOWED_RESOURCE_TYPES")
        data = self._validate_component_properties(dict(properties or {}))
        component_path = self._validate_component_path(f"{parent_path}/{node_name}", write=True)
        # Parent must exist and target must not; Sling POST must never create a missing chain.
        await self._get_node_json(parent_path)
        try:
            await self._get_node_json(component_path)
        except ValueError as exc:
            if "not found" not in str(exc):
                raise
        else:
            raise FileExistsError(f"Component already exists: {component_path}")
        post_data = {"jcr:primaryType": "nt:unstructured", "sling:resourceType": resource_type, **data}
        result = await self._post_form(component_path, post_data)
        return {
            "created": True,
            "path": component_path,
            "resourceType": resource_type,
            "propertiesApplied": data,
            "aemResponse": result,
        }

    async def update_component_properties(
        self, component_path: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        component_path = self._validate_component_path(component_path, write=True)
        data = self._validate_component_properties(dict(properties))
        if not data:
            raise ValueError("At least one component property must be supplied")
        existing = await self.get_component_properties(component_path)
        if not existing.get("resourceType"):
            raise ValueError("Target node is not a component with sling:resourceType")
        await self._post_form(component_path, data)
        final = await self.get_component_properties(component_path)
        return {"updated": True, **final}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.aem_base_url.rstrip("/"),
            auth=(self.settings.aem_username, self.settings.aem_password),
            timeout=self.settings.aem_timeout_seconds,
            verify=self.settings.aem_verify_ssl,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    async def _csrf_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        """
        Local AEM may enforce Granite CSRF protection for authenticated POST requests.
        Fetch a token when available. If the endpoint is unavailable, Basic Auth on
        a local SDK may still be accepted; return no token in that case.
        """
        try:
            r = await client.get("/libs/granite/csrf/token.json")
            if r.is_success:
                token = (r.json() or {}).get("token")
                if token:
                    return {"CSRF-Token": token}
        except Exception:
            pass
        return {}

    async def _post_form(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            headers = await self._csrf_headers(client)
            r = await client.post(path, data=data, headers=headers)
            if not r.is_success:
                raise RuntimeError(
                    f"AEM POST failed: HTTP {r.status_code}: {r.text[:1000]}"
                )
            content_type = r.headers.get("content-type", "")
            if "json" in content_type:
                return r.json()
            return {
                "statusCode": r.status_code,
                "location": r.headers.get("Location"),
                "body": r.text[:2000],
            }

    async def get_page_properties(self, page_path: str) -> dict[str, Any]:
        page_path = self._validate_read_path(page_path)
        content_path = f"{page_path}/jcr:content"
        url = f"{quote(content_path, safe='/')}.json"

        async with self._client() as client:
            response = await client.get(url)
            if response.status_code == 404:
                raise ValueError(f"Page or jcr:content not found: {page_path}")
            response.raise_for_status()
            data = response.json()

        return {
            "path": page_path,
            "contentPath": content_path,
            "properties": data,
        }

    async def search_pages(
        self,
        root: str,
        text: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        root = self._validate_read_path(root)
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        params: dict[str, str | int] = {
            "path": root,
            "type": "cq:Page",
            "p.offset": offset,
            "p.limit": limit,
            "p.guessTotal": "true",
            "orderby": "path",
            "p.hits": "selective",
            "p.properties": "jcr:path jcr:content/jcr:title jcr:content/cq:lastModified jcr:content/cq:lastModifiedBy",
        }
        if text:
            params["fulltext"] = text
            params["fulltext.relPath"] = "jcr:content"
        return await self._querybuilder(params)

    async def list_child_pages(self, root: str, limit: int = 50) -> dict[str, Any]:
        root = self._validate_read_path(root)
        limit = min(max(limit, 1), 100)
        params: dict[str, str | int] = {
            "path": root,
            "type": "cq:Page",
            "path.flat": "true",
            "p.limit": limit,
            "p.guessTotal": "true",
            "orderby": "path",
            "p.hits": "selective",
            "p.properties": "jcr:path jcr:content/jcr:title jcr:content/cq:lastModified",
        }
        return await self._querybuilder(params)

    async def find_component_usage(
        self,
        root: str,
        resource_type: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        root = self._validate_read_path(root)
        if not resource_type.strip():
            raise ValueError("resource_type is required")
        limit = min(max(limit, 1), 100)
        params: dict[str, str | int] = {
            "path": root,
            "property": "sling:resourceType",
            "property.value": resource_type,
            "p.limit": limit,
            "p.guessTotal": "true",
            "orderby": "path",
            "p.hits": "selective",
            "p.properties": "jcr:path sling:resourceType",
        }
        return await self._querybuilder(params)

    async def create_page(
        self,
        parent_path: str,
        name: str,
        title: str,
        template: str,
        resource_type: str,
    ) -> dict[str, Any]:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("name must be one valid page node name without '/'")

        parent_path = self._validate_write_path(parent_path)
        page_path = self._validate_write_path(f"{parent_path}/{name}")

        # Create the cq:Page node.
        await self._post_form(
            page_path,
            {
                "jcr:primaryType": "cq:Page",
            },
        )

        # Create/configure jcr:content separately. This keeps behavior transparent
        # and easy to inspect in CRXDE during the learning POC.
        content_result = await self._post_form(
            f"{page_path}/jcr:content",
            {
                "jcr:primaryType": "cq:PageContent",
                "jcr:title": title,
                "cq:template": template,
                "sling:resourceType": resource_type,
            },
        )

        return {
            "created": True,
            "path": page_path,
            "title": title,
            "template": template,
            "resourceType": resource_type,
            "aemResponse": content_result,
        }

    async def update_page_properties(
        self,
        page_path: str,
        title: str | None = None,
        description: str | None = None,
        resource_type: str | None = None,
    ) -> dict[str, Any]:
        page_path = self._validate_write_path(page_path)
        data: dict[str, Any] = {}

        if title is not None:
            data["jcr:title"] = title
        if description is not None:
            data["jcr:description"] = description
        if resource_type is not None:
            data["sling:resourceType"] = resource_type

        if not data:
            raise ValueError("At least one property value must be supplied")

        result = await self._post_form(f"{page_path}/jcr:content", data)
        return {
            "updated": True,
            "path": page_path,
            "changedProperties": list(data.keys()),
            "aemResponse": result,
        }

    async def set_page_property(
        self,
        page_path: str,
        property_name: str,
        value: str,
    ) -> dict[str, Any]:
        page_path = self._validate_write_path(page_path)

        if not property_name.strip():
            raise ValueError("property_name is required")
        if property_name.startswith(":"):
            raise ValueError("Sling POST control properties are not allowed")
        if property_name.startswith("jcr:") or property_name.startswith("sling:"):
            raise ValueError("Protected JCR/Sling properties are not allowed")
        if "/" in property_name:
            raise ValueError("This tool only sets direct jcr:content properties")

        result = await self._post_form(
            f"{page_path}/jcr:content",
            {property_name: value},
        )
        return {
            "updated": True,
            "path": page_path,
            "property": property_name,
            "value": value,
            "aemResponse": result,
        }

    async def move_page(
        self,
        source_path: str,
        destination_path: str,
        confirm: bool,
    ) -> dict[str, Any]:
        if not confirm:
            raise PermissionError("Move refused: call with confirm=true")
        source_path = self._validate_write_path(source_path)
        destination_path = self._validate_write_path(destination_path)

        result = await self._post_form(
            source_path,
            {
                ":operation": "move",
                ":dest": destination_path,
            },
        )
        return {
            "moved": True,
            "source": source_path,
            "destination": destination_path,
            "aemResponse": result,
        }

    async def delete_page(self, page_path: str, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise PermissionError("Delete refused: call with confirm=true")
        page_path = self._validate_write_path(page_path)

        result = await self._post_form(
            page_path,
            {
                ":operation": "delete",
            },
        )
        return {
            "deleted": True,
            "path": page_path,
            "aemResponse": result,
        }

    async def _querybuilder(self, params: dict[str, str | int]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.get("/bin/querybuilder.json", params=params)
            response.raise_for_status()
            payload = response.json()
        hits = payload.get("hits", [])
        return {
            "success": payload.get("success", True),
            "results": payload.get("results", len(hits)),
            "total": payload.get("total"),
            "more": payload.get("more", False),
            "offset": payload.get("offset", 0),
            "hits": hits,
        }

# Backward-compatible alias with the original read-only POC name.
AEMReadClient = AEMClient
