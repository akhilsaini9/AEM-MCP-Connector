from __future__ import annotations

from typing import Any

import pytest

from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.services.authoring import AuthoringService
from aem_mcp.services.dependencies import (
    DependencyService,
    ValidationService,
    normalize_internal_content_path,
)


def client() -> AEMClient:
    result = AEMClient()
    result.settings = Settings(_env_file=None, aem_allowed_roots="/content")
    return result


async def async_value(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_dependency_content_fragment_requires_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    walked = {
        "components": [
            {"path":"/content/site/p/jcr:content/cf","resourceType":"core/wcm/components/contentfragment/v1/contentfragment","properties":{"fragmentPath":"/content/dam/fragments/article"}},
            {"path":"/content/site/p/jcr:content/image","resourceType":"core/wcm/components/image/v3/image","properties":{"fileReference":"/content/dam/images/a.jpg"}},
            {"path":"/content/site/p/jcr:content/ambiguous","resourceType":"custom/reference","properties":{"path":"/content/dam/fragments/unknown"}},
        ],
        "incomplete":False,
        "incompleteReasons":[],
    }
    instance=client(); monkeypatch.setattr(instance,"walk_components",lambda *a,**k:async_value(walked))
    result=await DependencyService(instance).get_page_dependencies("/content/site/p")
    assert result["content_fragments"]==["/content/dam/fragments/article"]
    assert result["assets"]==["/content/dam/fragments/unknown","/content/dam/images/a.jpg"]
    assert not set(result["content_fragments"]) & set(result["assets"])


@pytest.mark.parametrize(
    ("value","expected"),
    [
        ("/content/site/page.html#section","/content/site/page"),
        ("/content/site/page.html?x=1","/content/site/page"),
        ("/content/site/page?x=1#section","/content/site/page"),
        ("/content/site/page","/content/site/page"),
        ("/content/site/my.file/page.html","/content/site/my.file/page"),
        ("https://example.com/content/site/page",None),
        ("//example.com/content/site/page",None),
        ("mailto:a@example.com",None),
        ("tel:+123",None),
        ("javascript:alert(1)",None),
        ("#section",None),
    ],
)
def test_normalize_internal_content_path(value: str, expected: str|None) -> None:
    assert normalize_internal_content_path(value)==expected


@pytest.mark.asyncio
async def test_validation_checks_only_normalized_internal_links(monkeypatch: pytest.MonkeyPatch) -> None:
    values=["/content/site/a.html#x","/content/site/b.html?q=1","/content/site/c?q=1#x","/content/site/d","https://example.com/content/site/e","mailto:a@b.com","tel:1","javascript:x","#x","//example.com/content/site/f"]
    walked={"components":[{"path":"/content/site/p/jcr:content/links","resourceType":"custom/links","properties":{f"link{i}":value for i,value in enumerate(values)}}],"incomplete":False,"incompleteReasons":[]}
    checked=[]; instance=client(); monkeypatch.setattr(instance,"walk_components",lambda *a,**k:async_value(walked))
    async def exists(path:str)->bool: checked.append(path); return True
    monkeypatch.setattr(instance,"node_exists",exists)
    await ValidationService(instance).validate_page("/content/site/p")
    assert checked==["/content/site/a","/content/site/b","/content/site/c","/content/site/d"]


@pytest.mark.asyncio
async def test_authoring_schema_filters_structural_nodes_and_models_multifields(monkeypatch: pytest.MonkeyPatch) -> None:
    dialog={
        "container":{"sling:resourceType":"granite/ui/components/coral/foundation/container","name":"./notAField"},
        "datasource":{"sling:resourceType":"granite/ui/components/coral/foundation/datasource","name":"./config"},
        "title":{"sling:resourceType":"granite/ui/components/coral/foundation/form/textfield","name":"./title"},
        "enabled":{"sling:resourceType":"granite/ui/components/coral/foundation/form/checkbox","name":"./enabled"},
        "simple":{"sling:resourceType":"granite/ui/components/coral/foundation/form/multifield","field":{"sling:resourceType":"granite/ui/components/coral/foundation/form/textfield","name":"./tags"}},
        "composite":{"sling:resourceType":"granite/ui/components/coral/foundation/form/multifield","composite":True,"name":"./items","field":{"sling:resourceType":"granite/ui/components/coral/foundation/container","title":{"sling:resourceType":"granite/ui/components/coral/foundation/form/textfield","name":"./title"},"link":{"sling:resourceType":"granite/ui/components/coral/foundation/form/pathfield","name":"./link"}}},
        "unsupported":{"sling:resourceType":"custom/widgets/specialinput","name":"./danger"},
    }
    nodes={"/apps/local/component":{},"/apps/local/component/cq:dialog":dialog}
    async def get(path:str)->dict[str,Any]:
        if path in nodes:return nodes[path]
        raise ValueError("not found")
    instance=client(); monkeypatch.setattr(instance,"_get_node_json",get)
    result=await AuthoringService(instance).schema("local/component"); fields={f["name"]:f for f in result["fields"]}
    assert {"title","enabled","tags","items"}==set(fields)
    assert not fields["tags"]["composite"] and fields["tags"]["child_fields"][0]["name"]=="tags"
    assert fields["items"]["composite"] and {f["name"] for f in fields["items"]["child_fields"]}=={"title","link"}
    assert "notAField" not in fields and "config" not in fields and "danger" not in fields
    assert any("specialinput" in warning and "unsupported" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_inherited_supported_field_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes={
        "/apps/local/component":{"sling:resourceSuperType":"base/component"},
        "/apps/base/component":{},
        "/apps/local/component/cq:dialog":{},
        "/apps/base/component/cq:dialog":{"description":{"sling:resourceType":"granite/ui/components/coral/foundation/form/textarea","name":"./description"}},
    }
    async def get(path:str)->dict[str,Any]:
        if path in nodes:return nodes[path]
        raise ValueError("not found")
    instance=client(); monkeypatch.setattr(instance,"_get_node_json",get)
    result=await AuthoringService(instance).schema("local/component")
    assert result["fields"]==[{"name":"description","label":"description","field_type":"textarea","required":False,"source":"inherited","multifield":False,"dialog_node_path":"/apps/base/component/cq:dialog/description"}]
