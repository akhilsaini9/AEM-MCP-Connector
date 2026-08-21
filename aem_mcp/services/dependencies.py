from __future__ import annotations
from typing import Any
from urllib.parse import urlsplit
from ..aem_client import AEMClient


def normalize_internal_content_path(value: str) -> str | None:
    """Return a repository path for an internal authored link, or None."""
    if not isinstance(value, str) or not value or value.startswith("#"):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    path = parsed.path
    if not path.startswith("/content/"):
        return None
    if path.endswith(".html"):
        path = path[:-5]
    return path


def _is_confirmed_content_fragment(
    property_name: str, component: dict[str, Any]
) -> bool:
    properties = component.get("properties", {})
    resource_type = str(component.get("resourceType", "")).lower()
    return (
        property_name in {"fragmentPath", "contentFragmentPath"}
        and (
            properties.get("contentFragment") is True
            or "contentfragment" in resource_type
            or "content-fragment" in resource_type
        )
    )


class DependencyService:
    def __init__(self, client: AEMClient) -> None:
        self.client = client

    async def get_page_dependencies(self, page_path: str, limit: int = 500) -> dict[str, Any]:
        page_path = self.client._validate_read_path(page_path)
        limit = min(max(limit, 1), 1000)
        walked = await self.client.walk_components(page_path, max_depth=20, limit=limit)
        assets: set[str] = set(); pages: set[str] = set(); xfs: set[str] = set(); cfs: set[str] = set(); types: set[str] = set()
        for component in walked["components"]:
            types.add(component["resourceType"])
            for property_name, value in component["properties"].items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if not isinstance(item, str): continue
                    path = normalize_internal_content_path(item)
                    if path is None: continue
                    if path.startswith("/content/dam/") and _is_confirmed_content_fragment(property_name, component): cfs.add(path)
                    elif path.startswith("/content/dam/"): assets.add(path)
                    elif path.startswith("/content/experience-fragments/"): xfs.add(path)
                    elif path.startswith("/content/") and path != page_path: pages.add(path.removesuffix(".html"))
        warnings = []
        if walked["incomplete"]: warnings.append("Dependency traversal was bounded by " + ", ".join(walked["incompleteReasons"]))
        return {"page_path": page_path, "assets": sorted(assets), "pages": sorted(pages), "experience_fragments": sorted(xfs), "content_fragments": sorted(cfs), "component_resource_types": sorted(types), "child_paths": [f"{page_path}/jcr:content"], "warnings": warnings}


class ValidationService:
    def __init__(self, client: AEMClient) -> None: self.client = client

    async def validate_page(self, page_path: str) -> dict[str, Any]:
        page_path = self.client._validate_read_path(page_path)
        walked = await self.client.walk_components(page_path, max_depth=20, limit=500)
        issues: list[dict[str, Any]] = []
        async def issue_missing(path: str, prop: str, target: str) -> None:
            if not await self.client.node_exists(target):
                issues.append({"severity":"error","code":"MISSING_REFERENCED_RESOURCE","message":f"Referenced resource does not exist: {target}","component_path":path,"property_name":prop})
        for component in walked["components"]:
            props = component["properties"]; rt = component["resourceType"].lower(); path = component["path"]
            asset = props.get("fileReference") or props.get("asset")
            if "image" in rt:
                if not asset: issues.append({"severity":"error","code":"MISSING_ASSET_REFERENCE","message":"Image component has no asset reference","component_path":path,"property_name":"fileReference"})
                elif isinstance(asset,str): await issue_missing(path,"fileReference",asset)
                decorative = props.get("isDecorative") is True or props.get("decorative") is True
                if not decorative and not str(props.get("alt","")).strip(): issues.append({"severity":"warning","code":"MISSING_ALT_TEXT","message":"Non-decorative image has no alt text","component_path":path,"property_name":"alt"})
            if "text" in rt and not str(props.get("text","")).strip(): issues.append({"severity":"warning","code":"EMPTY_TEXT","message":"Text component is empty","component_path":path,"property_name":"text"})
            for name,value in props.items():
                if isinstance(value,str):
                    target = normalize_internal_content_path(value)
                    if target is not None and not target.startswith("/content/dam/"):
                        await issue_missing(path,name,target)
        if walked["incomplete"]: issues.append({"severity":"warning","code":"VALIDATION_INCOMPLETE","message":"Validation traversal was bounded","component_path":f"{page_path}/jcr:content","property_name":""})
        errors=sum(i["severity"]=="error" for i in issues); warnings=sum(i["severity"]=="warning" for i in issues)
        return {"page_path":page_path,"valid":errors==0,"issues":issues,"summary":{"errors":errors,"warnings":warnings}}
