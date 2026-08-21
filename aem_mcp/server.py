from __future__ import annotations
from typing import Any
from mcp.server import MCPServer
from mcp.types import CallToolResult
from .aem_client import AEMClient
from .services.assets import AssetService
from .services.asset_preview import AssetPreviewService
from .services.authoring import AuthoringService
from .services.dependencies import DependencyService, ValidationService
from .services.publication import PublicationService
from .adobe_mcp.errors import AdobeMCPError, AdobeMCPToolNotAvailableError
from .adobe_mcp.sessions import adobe_mcp_sessions
from .audit import audit_adobe_mcp
from .config import get_settings

mcp = MCPServer(
    "custom-aem-crud",
    instructions=(
        "AEM local SDK MCP with read and guarded write tools. "
        "Writes only work when AEM_WRITE_ENABLED=true and only inside AEM_WRITE_ROOTS. "
        "Delete and move require confirm=true."
    ),
)

@mcp.tool()
async def get_page_properties(path: str) -> dict[str, Any]:
    """Return direct jcr:content properties for one AEM page."""
    return await AEMClient().get_page_properties(path)

@mcp.tool()
async def search_pages(
    root: str,
    text: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Search cq:Page nodes below an AEM root, optionally using full text."""
    return await AEMClient().search_pages(root, text, limit, offset)

@mcp.tool()
async def list_child_pages(root: str, limit: int = 50) -> dict[str, Any]:
    """List direct child cq:Page nodes."""
    return await AEMClient().list_child_pages(root, limit)

@mcp.tool()
async def find_component_usage(
    root: str,
    resource_type: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Find repository nodes using a specific sling:resourceType."""
    return await AEMClient().find_component_usage(root, resource_type, limit)

@mcp.tool()
async def list_components(
    page_path: str, max_depth: int = 10, limit: int = 200
) -> dict[str, Any]:
    """Recursively list components below a page's jcr:content."""
    return await AEMClient().list_components(page_path, max_depth, limit)

@mcp.tool()
async def get_component_properties(component_path: str) -> dict[str, Any]:
    """Return direct, bounded properties for an exact component path."""
    return await AEMClient().get_component_properties(component_path)

@mcp.tool()
async def find_components(
    page_path: str,
    resource_type: str | None = None,
    name_contains: str | None = None,
    property_name: str | None = None,
    property_value: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Find page components using optional resource type, name, and property filters."""
    return await AEMClient().find_components(
        page_path, resource_type, name_contains, property_name, property_value, limit
    )

@mcp.tool()
async def add_component(
    parent_path: str,
    node_name: str,
    resource_type: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a component below an existing writable container."""
    return await AEMClient().add_component(
        parent_path, node_name, resource_type, properties
    )

@mcp.tool()
async def update_component_properties(
    component_path: str, properties: dict[str, Any]
) -> dict[str, Any]:
    """Update safe direct properties on an existing writable component."""
    return await AEMClient().update_component_properties(component_path, properties)

@mcp.tool()
async def create_page(
    parent_path: str,
    name: str,
    title: str,
    template: str,
    resource_type: str,
) -> dict[str, Any]:
    """
    Create a cq:Page and its jcr:content under parent_path.
    Write access must be explicitly enabled in .env.
    """
    return await AEMClient().create_page(
        parent_path, name, title, template, resource_type
    )

@mcp.tool()
async def update_page_properties(
    page_path: str,
    title: str | None = None,
    description: str | None = None,
    resource_type: str | None = None,
) -> dict[str, Any]:
    """Update common direct properties on a page's jcr:content node."""
    return await AEMClient().update_page_properties(
        page_path, title, description, resource_type
    )

@mcp.tool()
async def set_page_property(
    page_path: str,
    property_name: str,
    value: str,
) -> dict[str, Any]:
    """Set one direct string property on a page's jcr:content node."""
    return await AEMClient().set_page_property(page_path, property_name, value)

@mcp.tool()
async def move_page(
    source_path: str,
    destination_path: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Move a page within allowed write roots. Must pass confirm=true."""
    return await AEMClient().move_page(source_path, destination_path, confirm)

@mcp.tool()
async def delete_page(
    page_path: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a page within allowed write roots. Must pass confirm=true."""
    return await AEMClient().delete_page(page_path, confirm)

@mcp.tool()
async def publish_page(page_path: str, include_references: bool = False, dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Publish an AEM page after bounded impact analysis. Defaults to dry-run; actual publication requires dry_run=false and confirm=true."""
    return await PublicationService(AEMClient()).page(page_path, "Activate", include_references, dry_run, confirm)

@mcp.tool()
async def unpublish_page(page_path: str, dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Unpublish an AEM page after impact analysis. Defaults to dry-run; actual unpublication requires dry_run=false and confirm=true."""
    return await PublicationService(AEMClient()).page(page_path, "Deactivate", False, dry_run, confirm)

@mcp.tool()
async def get_page_dependencies(page_path: str, limit: int = 500) -> dict[str, Any]:
    """Read bounded, reliably detectable page dependencies including DAM, internal content references, fragments, and component resource types."""
    return await DependencyService(AEMClient()).get_page_dependencies(page_path, limit)

@mcp.tool()
async def validate_page(page_path: str) -> dict[str, Any]:
    """Run deterministic, read-only AEM authoring checks for missing assets, alt text, empty text, and broken internal references."""
    return await ValidationService(AEMClient()).validate_page(page_path)

@mcp.tool()
async def search_assets(root: str = "/content/dam", text: str | None = None, mime_type: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Search AEM DAM under an allowed root using text and optional MIME filters. This is read-only and bounded."""
    return await AssetService(AEMClient()).search(root, text, mime_type, limit, offset)

@mcp.tool()
async def get_asset_metadata(asset_path: str) -> dict[str, Any]:
    """Return bounded, sanitized metadata and inexpensive rendition information for one readable AEM DAM asset."""
    return await AssetService(AEMClient()).metadata(asset_path)

@mcp.tool(structured_output=False)
async def get_asset_preview(asset_path: str, rendition: str | None = None, max_bytes: int | None = None) -> CallToolResult:
    """Retrieve a safe preview of an AEM DAM image or PDF. Uses a web-friendly rendition where possible and never exposes AEM credentials."""
    return await AssetPreviewService(AEMClient()).preview(asset_path, rendition, max_bytes)

@mcp.tool()
async def upload_asset(dam_folder: str, file_name: str, content_base64: str, mime_type: str | None = None, metadata: dict[str, Any] | None = None, overwrite: bool = False, dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Upload base64 content to local AEM DAM through an isolated strategy. Defaults to dry-run; validates size/MIME/name and requires confirmation."""
    return await AssetService(AEMClient()).upload(dam_folder, file_name, content_base64, mime_type, metadata, overwrite, dry_run, confirm)

@mcp.tool()
async def find_asset_usage(asset_path: str, root: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Find bounded page/component fileReference usages of an AEM DAM asset using QueryBuilder. This is read-only."""
    return await AssetService(AEMClient()).usage(asset_path, root, limit)

@mcp.tool()
async def update_asset_metadata(asset_path: str, properties: dict[str, Any], dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Update explicitly allowed DAM metadata after usage impact analysis. Defaults to dry-run; actual writes require confirmation."""
    return await AssetService(AEMClient()).update(asset_path, properties, dry_run, confirm)

@mcp.tool()
async def publish_asset(asset_path: str, dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Publish an AEM DAM asset after usage impact analysis. Defaults to dry-run; actual publication requires confirmation."""
    client=AEMClient(); usages=(await AssetService(client).usage(asset_path, limit=100))["usages"]
    return await PublicationService(client).asset(asset_path, "Activate", dry_run, confirm, usages)

@mcp.tool()
async def unpublish_asset(asset_path: str, dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Unpublish an AEM DAM asset after usage impact analysis. Defaults to dry-run; actual unpublication requires confirmation."""
    client=AEMClient(); usages=(await AssetService(client).usage(asset_path, limit=100))["usages"]
    return await PublicationService(client).asset(asset_path, "Deactivate", dry_run, confirm, usages)

@mcp.tool()
async def get_component_authoring_schema(resource_type: str) -> dict[str, Any]:
    """Inspect a component cq:dialog and inherited resource-super-type dialogs to return normalized fields visible to an AEM author."""
    return await AuthoringService(AEMClient()).schema(resource_type)

@mcp.tool()
async def get_component_definition(resource_type: str) -> dict[str, Any]:
    """Read a bounded AEM component definition and its safe resource-super-type inheritance chain."""
    return await AuthoringService(AEMClient()).definition(resource_type)

@mcp.tool()
async def list_allowed_components(container_path: str, limit: int = 200) -> dict[str, Any]:
    """Resolve components explicitly allowed by the authored container's content policy; returns warnings when exact policy resolution is unavailable."""
    return await AuthoringService(AEMClient()).allowed_components(container_path, limit)


def _adobe_error(exc: AdobeMCPError) -> dict[str, Any]:
    return exc.safe_result()


@mcp.tool()
async def get_adobe_mcp_connection_status() -> dict[str, Any]:
    """Return non-sensitive status for the current user's downstream Adobe AEM MCP session. Read-only."""
    try:
        return await adobe_mcp_sessions.status()
    except AdobeMCPError as exc:
        return _adobe_error(exc)


@mcp.tool()
async def connect_adobe_mcp() -> dict[str, Any]:
    """Start or resume the current user's Adobe AEM MCP browser authorization flow. Never returns tokens."""
    try:
        client = await adobe_mcp_sessions.get_client()
        key = adobe_mcp_sessions.session_key()
        async with audit_adobe_mcp(get_settings(), "connect_adobe_mcp", None, key) as audit:
            result = await client.connect()
            audit["success"] = bool(result.get("connected") or result.get("authorization_started"))
            return result
    except AdobeMCPError as exc:
        return _adobe_error(exc)


@mcp.tool()
async def disconnect_adobe_mcp() -> dict[str, Any]:
    """Disconnect and clear only the current user's locally stored Adobe MCP session."""
    try:
        return await adobe_mcp_sessions.disconnect()
    except AdobeMCPError as exc:
        return _adobe_error(exc)


@mcp.tool()
async def list_adobe_mcp_tools() -> dict[str, Any]:
    """List sanitized tools advertised to the current user by Adobe's AEM MCP server. Read-only."""
    try:
        client = await adobe_mcp_sessions.get_client()
        key = adobe_mcp_sessions.session_key()
        async with audit_adobe_mcp(get_settings(), "list_adobe_mcp_tools", None, key) as audit:
            tools = await client.list_tools()
            audit["success"] = True
            return {"tools": tools, "count": len(tools)}
    except AdobeMCPError as exc:
        return _adobe_error(exc)


@mcp.tool()
async def list_aem_cloud_environments() -> dict[str, Any]:
    """Invoke the explicitly configured and allowlisted Adobe Cloud Manager environment-listing tool. Read-only."""
    settings = get_settings()
    upstream = settings.adobe_mcp_environments_tool.strip()
    if not upstream:
        return _adobe_error(AdobeMCPToolNotAvailableError(
            "No upstream environment tool is configured. Discover Adobe tools first, then set ADOBE_MCP_ENVIRONMENTS_TOOL to the verified read-only tool name."
        ))
    try:
        client = await adobe_mcp_sessions.get_client()
        key = adobe_mcp_sessions.session_key()
        async with audit_adobe_mcp(settings, "list_aem_cloud_environments", upstream, key) as audit:
            result = await client.call_tool(upstream, {})
            audit["success"] = True
            return {"upstream_tool": upstream, "result": result}
    except AdobeMCPError as exc:
        return _adobe_error(exc)
