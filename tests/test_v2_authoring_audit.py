from __future__ import annotations
import json
import logging
import pytest
from aem_mcp.aem_client import AEMClient
from aem_mcp.audit import audit_operation
from aem_mcp.config import Settings
from aem_mcp.services.authoring import AuthoringService

def client(depth=10):
    c=AEMClient(); c.settings=Settings(_env_file=None,aem_allowed_roots="/content",aem_component_dialog_max_inheritance_depth=depth); return c

@pytest.mark.asyncio
async def test_definition_and_inheritance(monkeypatch):
    c=client(); nodes={"/apps/sigma/components/image":{"jcr:title":"Image","componentGroup":"Sigma","sling:resourceSuperType":"core/image"},"/apps/core/image":{"jcr:title":"Core"},"/apps/sigma/components/image/cq:dialog":{}}
    async def get(p):
        if p in nodes:return nodes[p]
        raise ValueError("not found")
    monkeypatch.setattr(c,"_get_node_json",get); result=await AuthoringService(c).definition("sigma/components/image")
    assert result["title"]=="Image" and result["component_group"]=="Sigma" and result["inheritance_chain"]==["sigma/components/image","core/image"] and result["has_dialog"]

@pytest.mark.asyncio
async def test_schema_local_inherited_nested_multifield_and_warning(monkeypatch):
    c=client(); nodes={
      "/apps/sigma/image":{"sling:resourceSuperType":"core/image","items":{}}, "/apps/core/image":{},
      "/apps/sigma/image/cq:dialog":{"content":{}}, "/apps/sigma/image/cq:dialog/content":{"alt":{"sling:resourceType":"granite/ui/components/coral/foundation/form/textfield","name":"./alt","fieldLabel":"Alt"},"multi":{"sling:resourceType":"granite/ui/components/coral/foundation/form/multifield","name":"./items"}},
      "/apps/sigma/image/cq:dialog/content/alt":{"sling:resourceType":"granite/ui/components/coral/foundation/form/textfield","name":"./alt","fieldLabel":"Alt"}, "/apps/sigma/image/cq:dialog/content/multi":{"sling:resourceType":"granite/ui/components/coral/foundation/form/multifield","name":"./items"},
      "/apps/core/image/cq:dialog":{"file":{"sling:resourceType":"granite/ui/components/coral/foundation/form/pathfield","name":"./fileReference"},"odd":{"sling:resourceType":"custom/widget","name":"./odd"}},
      "/apps/core/image/cq:dialog/file":{"sling:resourceType":"granite/ui/components/coral/foundation/form/pathfield","name":"./fileReference"}, "/apps/core/image/cq:dialog/odd":{"sling:resourceType":"custom/widget","name":"./odd"},
    }
    async def get(p):
        if p in nodes:return nodes[p]
        raise ValueError("not found")
    monkeypatch.setattr(c,"_get_node_json",get); result=await AuthoringService(c).schema("sigma/image"); by={f["name"]:f for f in result["fields"]}
    assert by["alt"]["source"]=="local" and by["fileReference"]["source"]=="inherited" and by["items"]["multifield"] and any("Unsupported" in w for w in result["warnings"])

@pytest.mark.asyncio
async def test_inheritance_loop_and_max_depth(monkeypatch):
    c=client(2); nodes={"/apps/a":{"sling:resourceSuperType":"b"},"/apps/b":{"sling:resourceSuperType":"a"}}
    async def get(p):
        if p in nodes:return nodes[p]
        raise ValueError("not found")
    monkeypatch.setattr(c,"_get_node_json",get); result=await AuthoringService(c).definition("a")
    assert any("depth" in w.lower() or "loop" in w.lower() for w in result["warnings"])

@pytest.mark.asyncio
async def test_allowed_components_policy_and_missing_warning(monkeypatch):
    c=client(); nodes={"/content/site/p/jcr:content/root":{"cq:policy":"/conf/site/policy"},"/conf/site/policy":{"components":["sigma/text"]},"/apps/sigma/text":{"jcr:title":"Text","componentGroup":"Sigma"}}
    async def get(p):
        if p in nodes:return nodes[p]
        raise ValueError("not found")
    monkeypatch.setattr(c,"_get_node_json",get); svc=AuthoringService(c); result=await svc.allowed_components("/content/site/p/jcr:content/root")
    assert result["components"][0]["resource_type"]=="sigma/text"
    nodes["/content/site/p/jcr:content/root"]={}; assert (await svc.allowed_components("/content/site/p/jcr:content/root"))["warnings"]

@pytest.mark.asyncio
async def test_audit_logs_preview_and_sanitized_error(caplog):
    settings=Settings(_env_file=None,mcp_audit_log_enabled=True)
    with caplog.at_level(logging.INFO,logger="aem_mcp.audit"):
        async with audit_operation(settings,"upload_asset","/content/dam/a",True,False) as state: state.update(result="preview",success=True)
        with pytest.raises(RuntimeError):
            async with audit_operation(settings,"publish_asset","/content/dam/a",False,True): raise RuntimeError("safe failure")
    records=[json.loads(r.message) for r in caplog.records]; assert records[0]["dry_run"] and records[0]["success"] and records[1]["error"]=="RuntimeError: safe failure"
    assert "Authorization" not in caplog.text and "CSRF" not in caplog.text
