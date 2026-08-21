from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, TextContent

from ..aem_client import AEMClient, BinaryResponse, BinaryTooLargeError
from ..audit import audit_read
from .assets import AssetService


IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
PDF_MIME_TYPE = "application/pdf"
_RENDITION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]*")


class PreviewMimeError(ValueError):
    """Raised when AEM identifies a rendition as a non-preview MIME type."""


@dataclass(frozen=True)
class PreviewCandidate:
    name: str
    path: str
    expected_mime_type: str | None = None


class AssetPreviewService:
    def __init__(self, client: AEMClient) -> None:
        self.client = client

    @staticmethod
    def _validate_rendition_name(name: str) -> str:
        if not _RENDITION_NAME.fullmatch(name or "") or name in {".", ".."}:
            raise ValueError("rendition must be a safe rendition name")
        return name

    async def _renditions(self, asset_path: str) -> list[PreviewCandidate]:
        root = f"{asset_path}/jcr:content/renditions"
        try:
            tree = await self.client._get_node_json(root)
        except ValueError as exc:
            if "not found" in str(exc):
                return []
            raise
        candidates: list[PreviewCandidate] = []
        for name, value in tree.items():
            if not isinstance(value, dict) or not _RENDITION_NAME.fullmatch(name) or name in {".", ".."}:
                continue
            content = value.get("jcr:content") if isinstance(value.get("jcr:content"), dict) else value
            mime = content.get("jcr:mimeType") if isinstance(content, dict) else None
            candidates.append(PreviewCandidate(name, f"{root}/{name}", mime.lower() if isinstance(mime, str) else None))
        return candidates[:100]

    @staticmethod
    def _rank_image(candidates: list[PreviewCandidate]) -> list[PreviewCandidate]:
        def rank(candidate: PreviewCandidate) -> tuple[int, str]:
            name = candidate.name.lower()
            if "web" in name:
                return (0, name)
            if "thumbnail" in name or "thumb" in name:
                return (1, name)
            if name == "original":
                return (3, name)
            return (2, name)
        return sorted(candidates, key=rank)

    async def _fetch_candidate(
        self, candidate: PreviewCandidate, max_bytes: int, allowed: set[str]
    ) -> BinaryResponse:
        binary = await self.client.get_binary(candidate.path, max_bytes)
        mime = binary.mime_type
        if mime == "text/html" or mime not in allowed:
            raise PreviewMimeError(f"AEM rendition returned unsupported MIME type: {mime or '[missing]'}")
        if candidate.expected_mime_type and candidate.expected_mime_type != mime:
            raise PreviewMimeError("AEM rendition MIME type did not match trusted rendition metadata")
        return binary

    async def preview(
        self, asset_path: str, rendition: str | None = None, max_bytes: int | None = None
    ) -> CallToolResult:
        path = self.client.validate_dam_read_path(asset_path)
        configured_limit = self.client.settings.aem_max_preview_bytes
        if configured_limit < 1:
            raise ValueError("AEM_MAX_PREVIEW_BYTES must be positive")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        effective_limit = min(max_bytes or configured_limit, configured_limit)
        allowed = set(self.client.settings.preview_allowed_mime_types)
        try:
            metadata = await AssetService(self.client).metadata(path)
            asset_mime = str(metadata.get("mime_type") or "").lower()
            if asset_mime not in allowed:
                raise ValueError(f"Asset MIME type is not previewable: {asset_mime or '[missing]'}")
            discovered = await self._renditions(path)
        except Exception:
            # Failures before binary selection still receive one metadata-only audit event.
            async with audit_read(self.client.settings, "get_asset_preview", path):
                raise
        warnings: list[str] = []
        requested = self._validate_rendition_name(rendition) if rendition is not None else None

        async with audit_read(self.client.settings, "get_asset_preview", path) as audit:
            if asset_mime in IMAGE_MIME_TYPES:
                if requested:
                    selected = next((item for item in discovered if item.name == requested), None)
                    if selected is None:
                        raise ValueError(f"Requested rendition not found: {requested}")
                    candidates = [selected]
                else:
                    candidates = self._rank_image(discovered)
                if not any(item.name == "original" for item in candidates):
                    candidates.append(PreviewCandidate("original", f"{path}/jcr:content/renditions/original", asset_mime))
                binary: BinaryResponse | None = None
                selected: PreviewCandidate | None = None
                for candidate in candidates:
                    try:
                        binary = await self._fetch_candidate(candidate, effective_limit, allowed & IMAGE_MIME_TYPES)
                        selected = candidate
                        break
                    except BinaryTooLargeError:
                        if requested:
                            raise
                        continue
                    except PreviewMimeError:
                        if requested:
                            raise
                        continue
                if binary is None or selected is None:
                    raise BinaryTooLargeError("No image rendition fits within AEM_MAX_PREVIEW_BYTES")
                if selected.name == "original":
                    warnings.append("No suitable smaller rendition was available; using the bounded original")
                result_metadata = self._metadata(metadata, binary, selected.name, "image", warnings)
                audit.update(success=True, selected_rendition=selected.name, mime_type=binary.mime_type, byte_count=binary.content_length)
                return CallToolResult(
                    content=[
                        TextContent(type="text", text=json.dumps(result_metadata, separators=(",", ":"))),
                        ImageContent(type="image", data=base64.b64encode(binary.content).decode("ascii"), mimeType=binary.mime_type),
                    ],
                    structuredContent=result_metadata,
                )

            if asset_mime != PDF_MIME_TYPE:
                raise ValueError(f"Asset MIME type is not previewable: {asset_mime}")
            pdf_candidate = PreviewCandidate("original", f"{path}/jcr:content/renditions/original", PDF_MIME_TYPE)
            if requested:
                match = next((item for item in discovered if item.name == requested), None)
                if match is None:
                    raise ValueError(f"Requested rendition not found: {requested}")
                pdf_candidate = match
            pdf = await self._fetch_candidate(pdf_candidate, effective_limit, {PDF_MIME_TYPE})
            thumbnail = None
            thumbnail_candidate = None
            for candidate in self._rank_image([item for item in discovered if item.name != "original"]):
                try:
                    thumbnail = await self._fetch_candidate(candidate, effective_limit, allowed & IMAGE_MIME_TYPES)
                    thumbnail_candidate = candidate
                    break
                except (BinaryTooLargeError, PreviewMimeError):
                    continue
            result_metadata = self._metadata(metadata, pdf, pdf_candidate.name, "embedded_resource", warnings)
            result_metadata["page_count"] = metadata.get("metadata", {}).get("xmpTPg:NPages") or metadata.get("metadata", {}).get("dam:PageCount")
            result_metadata["thumbnail_available"] = thumbnail is not None
            content: list[Any] = [
                TextContent(type="text", text=json.dumps(result_metadata, separators=(",", ":"))),
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=f"aem-dam://asset/{quote(path, safe='')}",
                        mimeType=PDF_MIME_TYPE,
                        blob=base64.b64encode(pdf.content).decode("ascii"),
                    ),
                ),
            ]
            if thumbnail is not None:
                content.append(ImageContent(type="image", data=base64.b64encode(thumbnail.content).decode("ascii"), mimeType=thumbnail.mime_type))
                result_metadata["thumbnail_rendition"] = thumbnail_candidate.name
            audit.update(success=True, selected_rendition=pdf_candidate.name, mime_type=pdf.mime_type, byte_count=pdf.content_length)
            return CallToolResult(content=content, structuredContent=result_metadata)

    @staticmethod
    def _metadata(asset: dict[str, Any], binary: BinaryResponse, rendition: str, preview_type: str, warnings: list[str]) -> dict[str, Any]:
        return {
            "asset_path": asset["path"],
            "mime_type": binary.mime_type,
            "rendition_name": rendition,
            "width": asset.get("width"),
            "height": asset.get("height"),
            "content_length": binary.content_length,
            "file_name": asset.get("name"),
            "preview_type": preview_type,
            "warnings": warnings,
        }
