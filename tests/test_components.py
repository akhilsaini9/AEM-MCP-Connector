from __future__ import annotations

from typing import Any
import pytest

from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings


TREE = {
    "jcr:primaryType": "cq:PageContent",
    "root": {
        "jcr:primaryType": "nt:unstructured",
        "sling:resourceType": "sigma/components/container",
        "responsivegrid": {
            "sling:resourceType": "wcm/foundation/components/responsivegrid",
            "text_123": {"sling:resourceType": "sigma/components/content/text", "text": "Hello", "textIsRich": True, "jcr:primaryType": "nt:unstructured"},
            "image_1": {"sling:resourceType": "sigma/components/content/image", "fileReference": "/content/dam/a.jpg", "alt": "A"},
        },
    },
}


def client(**settings: Any) -> AEMClient:
    result = AEMClient()
    result.settings = Settings(_env_file=None, aem_allowed_roots="/content", **settings)
    return result


def mock_tree(monkeypatch: pytest.MonkeyPatch, instance: AEMClient) -> None:
    async def get_node(path: str) -> dict[str, Any]:
        if path.endswith("/jcr:content"):
            return TREE
        if path.endswith("/jcr:content/root"):
            return TREE["root"]
        if path.endswith("/jcr:content/root/responsivegrid"):
            return TREE["root"]["responsivegrid"]
        if path.endswith("text_123"):
            return TREE["root"]["responsivegrid"]["text_123"]
        if path.endswith("image_1"):
            return TREE["root"]["responsivegrid"]["image_1"]
        raise ValueError(f"AEM node not found: {path}")
    monkeypatch.setattr(instance, "_get_node_json", get_node)


@pytest.mark.asyncio
async def test_component_traversal_and_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = client()
    mock_tree(monkeypatch, instance)
    result = await instance.list_components("/content/sigma/page")
    assert [item["name"] for item in result["components"]] == ["root", "responsivegrid", "text_123", "image_1"]
    text = result["components"][2]
    assert text["properties"]["text"] == "Hello"
    assert "jcr:primaryType" not in text["properties"]
    exact = await instance.get_component_properties(text["path"])
    assert exact["resourceType"] == "sigma/components/content/text"


@pytest.mark.asyncio
async def test_find_component_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = client()
    mock_tree(monkeypatch, instance)
    by_type = await instance.find_components("/content/sigma/page", resource_type="sigma/components/content/text")
    by_name = await instance.find_components("/content/sigma/page", name_contains="IMAGE")
    exists = await instance.find_components("/content/sigma/page", property_name="text")
    value = await instance.find_components("/content/sigma/page", property_name="text", property_value="Hello")
    assert [x["name"] for x in by_type["components"]] == ["text_123"]
    assert [x["name"] for x in by_name["components"]] == ["image_1"]
    assert exists["count"] == value["count"] == 1


@pytest.mark.asyncio
async def test_limit_and_max_depth_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = client()
    mock_tree(monkeypatch, instance)
    limited = await instance.list_components("/content/sigma/page", limit=1)
    shallow = await instance.list_components("/content/sigma/page", max_depth=1)
    assert limited["count"] == 1 and "limit" in limited["incompleteReasons"]
    assert shallow["count"] == 1 and "max_depth" in shallow["incompleteReasons"]


@pytest.mark.asyncio
async def test_add_write_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = client(aem_write_enabled=False, aem_write_roots="/content/sigma/us/en/mcp")
    with pytest.raises(PermissionError, match="disabled"):
        await disabled.add_component("/content/sigma/us/en/mcp/page/jcr:content/root", "text", "sigma/components/content/text")

    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp")
    with pytest.raises(PermissionError, match="outside"):
        await instance.add_component("/content/sigma/us/en/live/page/jcr:content/root", "text", "sigma/components/content/text")
    for invalid in ("../text", "a/b", ".", "..", ""):
        with pytest.raises(ValueError):
            await instance.add_component("/content/sigma/us/en/mcp/page/jcr:content/root", invalid, "sigma/components/content/text")


@pytest.mark.asyncio
async def test_update_write_safety_and_protected_properties() -> None:
    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp")
    with pytest.raises(PermissionError, match="outside"):
        await instance.update_component_properties("/content/sigma/us/en/live/page/jcr:content/root/text", {"text": "x"})
    for protected in ("jcr:primaryType", "sling:resourceType", "jcr:title", ":operation"):
        with pytest.raises(ValueError, match="Protected|Invalid"):
            instance._validate_component_properties({protected: "x"})


@pytest.mark.asyncio
async def test_resource_type_allowlist_is_enforced() -> None:
    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp", aem_component_allowed_resource_types="sigma/components/content/text")
    with pytest.raises(PermissionError, match="not allowed"):
        await instance.add_component("/content/sigma/us/en/mcp/page/jcr:content/root", "image", "sigma/components/content/image")


def test_component_path_traversal_rejected() -> None:
    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp")
    with pytest.raises(ValueError, match="traversal"):
        instance._validate_component_path("/content/sigma/us/en/mcp/page/jcr:content/root/../text", write=True)


def test_component_parent_path_allows_bootstrap_and_descendant() -> None:
    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp")
    bootstrap = "/content/sigma/us/en/mcp/page/jcr:content"
    descendant = bootstrap + "/root"
    assert instance._validate_component_path(bootstrap, write=True) == bootstrap
    assert instance._validate_component_path(descendant, write=True) == descendant


def test_component_parent_path_rejects_page_outside_write_roots() -> None:
    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp")
    with pytest.raises(PermissionError, match="outside AEM_WRITE_ROOTS"):
        instance._validate_component_path(
            "/content/sigma/us/en/live/page/jcr:content", write=True
        )


@pytest.mark.parametrize(
    "path",
    [
        "/content/sigma/us/en/mcp/page/jcr:content-invalid/root",
        "/content/sigma/us/en/mcp/page/prefix-jcr:content/root",
        "/content/sigma/us/en/mcp/page/jcr:content/root/jcr:content/child",
        "/jcr:content/root",
    ],
)
def test_component_parent_path_rejects_malformed_jcr_content(path: str) -> None:
    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp")
    with pytest.raises(ValueError):
        instance._validate_component_path(path, write=True)


@pytest.mark.parametrize(
    "path",
    [
        "/content/sigma/us/en/mcp/page/../other/jcr:content",
        "/content/sigma/us/en/mcp/page/jcr:content/root/../child",
        "/content/sigma/us/en/mcp/page/jcr:content/./child",
    ],
)
def test_component_parent_path_rejects_traversal_attempts(path: str) -> None:
    instance = client(aem_write_enabled=True, aem_write_roots="/content/sigma/us/en/mcp")
    with pytest.raises(ValueError, match="traversal"):
        instance._validate_component_path(path, write=True)
