from __future__ import annotations
from typing import Any
import pytest
from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.services.dependencies import DependencyService, ValidationService
from aem_mcp.services.publication import PublicationService

def client(**kw:Any)->AEMClient:
    c=AEMClient(); c.settings=Settings(_env_file=None,aem_allowed_roots="/content",aem_write_roots="/content/site",aem_publish_allowed_roots="/content/site",**kw); return c

def walk_result()->dict[str,Any]:
    return {"components":[
        {"path":"/content/site/p/jcr:content/image","resourceType":"x/image","properties":{"fileReference":"/content/dam/a.jpg","alt":""}},
        {"path":"/content/site/p/jcr:content/text","resourceType":"x/text","properties":{"text":"","link":"/content/site/other.html"}},
    ],"incomplete":False,"incompleteReasons":[]}

@pytest.mark.asyncio
async def test_dependencies_extract_and_bound(monkeypatch):
    c=client(); seen={}
    async def walk(path,max_depth,limit): seen["limit"]=limit; return walk_result()
    monkeypatch.setattr(c,"walk_components",walk)
    result=await DependencyService(c).get_page_dependencies("/content/site/p",7)
    assert result["assets"]==["/content/dam/a.jpg"] and result["pages"]==["/content/site/other"] and result["component_resource_types"]==["x/image","x/text"] and seen["limit"]==7

@pytest.mark.asyncio
async def test_validation_deterministic_checks(monkeypatch):
    c=client(); monkeypatch.setattr(c,"walk_components",lambda *a,**k: _async(walk_result())); monkeypatch.setattr(c,"node_exists",lambda p:_async(False))
    result=await ValidationService(c).validate_page("/content/site/p")
    codes={i["code"] for i in result["issues"]}; assert {"MISSING_ALT_TEXT","EMPTY_TEXT","MISSING_REFERENCED_RESOURCE"}<=codes and not result["valid"]

@pytest.mark.asyncio
async def test_decorative_image_has_no_alt_issue(monkeypatch):
    c=client(); data=walk_result(); data["components"]=[{"path":"/content/site/p/jcr:content/i","resourceType":"x/image","properties":{"fileReference":"/content/dam/a.jpg","isDecorative":True}}]
    monkeypatch.setattr(c,"walk_components",lambda *a,**k:_async(data)); monkeypatch.setattr(c,"node_exists",lambda p:_async(True))
    assert not (await ValidationService(c).validate_page("/content/site/p"))["issues"]

@pytest.mark.asyncio
async def test_publish_dry_run_and_confirmation(monkeypatch):
    c=client(aem_write_enabled=True); monkeypatch.setattr(c,"walk_components",lambda *a,**k:_async(walk_result())); calls=[]
    class Strategy:
        async def replicate(self,*args): calls.append(args); return {"ok":True}
    svc=PublicationService(c,Strategy()); preview=await svc.page("/content/site/p","Activate",False,True,False)
    rejected=await svc.page("/content/site/p","Activate",False,False,False)
    assert preview["dry_run"] and rejected["success"] is False and calls==[]
    actual=await svc.page("/content/site/p","Activate",False,False,True); assert actual["success"] and len(calls)==1

@pytest.mark.asyncio
async def test_publish_roots_and_write_enabled_enforced(monkeypatch):
    c=client(aem_write_enabled=True)
    with pytest.raises(PermissionError): await PublicationService(c).page("/content/other/p","Activate",False,False,True)
    disabled=client(aem_write_enabled=False)
    with pytest.raises(PermissionError): await PublicationService(disabled).page("/content/site/p","Deactivate",False,False,True)

async def _async(value): return value
