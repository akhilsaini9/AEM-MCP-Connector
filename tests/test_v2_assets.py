from __future__ import annotations
import base64
from typing import Any
import pytest
from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.services.assets import AssetService
from aem_mcp.services.publication import PublicationService

def client(**kw:Any)->AEMClient:
    c=AEMClient(); c.settings=Settings(_env_file=None,aem_allowed_roots="/content",aem_write_roots="/content/dam",aem_dam_read_roots="/content/dam",aem_dam_write_roots="/content/dam",**kw); return c
async def av(v): return v

@pytest.mark.asyncio
async def test_search_filters_limit_and_root(monkeypatch):
    c=client(aem_max_asset_search_limit=10); seen={}
    async def query(p): seen.update(p); return {"hits":[{"path":"/content/dam/a.jpg","jcr:content/metadata/dc:format":"image/jpeg"}],"more":False}
    monkeypatch.setattr(c,"_querybuilder",query); result=await AssetService(c).search(text="budda",mime_type="image/jpeg",limit=999)
    assert result["limit"]==10 and seen["fulltext"]=="budda" and seen["property.value"]=="image/jpeg"
    with pytest.raises(ValueError): await AssetService(c).search("/content/site")

@pytest.mark.asyncio
async def test_metadata_optional_and_safe(monkeypatch):
    c=client()
    async def node(p):
        if p.endswith("metadata"): return {"dc:title":"A","jcr:uuid":"secret","dc:format":"image/jpeg"}
        if p.endswith("renditions"): raise ValueError("not found")
        return {"jcr:created":"today"}
    monkeypatch.setattr(c,"_get_node_json",node); result=await AssetService(c).metadata("/content/dam/a.jpg")
    assert result["title"]=="A" and "jcr:uuid" not in result["metadata"] and result["renditions"]==[]

@pytest.mark.asyncio
async def test_usage_is_bounded(monkeypatch):
    c=client(aem_max_asset_usage_limit=2); monkeypatch.setattr(c,"_querybuilder",lambda p:av({"hits":[{"path":"/content/site/p/jcr:content/i"}],"more":True}))
    result=await AssetService(c).usage("/content/dam/a.jpg",limit=99); assert result["limit"]==2 and result["usages"][0]["page_path"]=="/content/site/p"

@pytest.mark.asyncio
async def test_upload_safety_dry_run_confirm_and_actual(monkeypatch):
    c=client(aem_write_enabled=True); monkeypatch.setattr(c,"node_exists",lambda p:av(False)); calls=[]
    class Upload:
        async def upload(self,*args,**kwargs): calls.append((args,kwargs)); return {"ok":True}
    svc=AssetService(c,Upload()); data=base64.b64encode(b"abc").decode()
    assert (await svc.upload("/content/dam","a.jpg",data,"image/jpeg"))["dry_run"] and not calls
    assert not (await svc.upload("/content/dam","a.jpg",data,"image/jpeg",dry_run=False))["success"] and not calls
    assert (await svc.upload("/content/dam","a.jpg",data,"image/jpeg",dry_run=False,confirm=True))["success"] and calls

@pytest.mark.asyncio
async def test_upload_rejects_name_mime_size_existing_and_root(monkeypatch):
    c=client(aem_max_asset_upload_bytes=2); svc=AssetService(c); data=base64.b64encode(b"abc").decode(); monkeypatch.setattr(c,"node_exists",lambda p:av(False))
    for name in ("../a.jpg","a/b.jpg",".."):
        with pytest.raises(ValueError): await svc.upload("/content/dam",name,data,"image/jpeg")
    with pytest.raises(ValueError): await svc.upload("/content/dam","a.exe",data,"application/octet-stream")
    with pytest.raises(ValueError): await svc.upload("/content/dam","a.jpg",data,"image/jpeg")
    with pytest.raises(ValueError): await svc.upload("/content/site","a.jpg",base64.b64encode(b"a").decode(),"image/jpeg")
    c.settings.aem_max_asset_upload_bytes=10; monkeypatch.setattr(c,"node_exists",lambda p:av(True))
    with pytest.raises(FileExistsError): await svc.upload("/content/dam","a.jpg",data,"image/jpeg")

@pytest.mark.asyncio
async def test_metadata_update_safety_and_confirmation(monkeypatch):
    c=client(aem_write_enabled=True); svc=AssetService(c); monkeypatch.setattr(svc,"usage",lambda *a,**k:av({"usages":[]})); calls=[]; monkeypatch.setattr(c,"post_form_unchecked",lambda *a: (calls.append(a),av({"ok":True}))[1])
    with pytest.raises(ValueError): await svc.update("/content/dam/a.jpg",{"jcr:title":"x"})
    assert (await svc.update("/content/dam/a.jpg",{"dc:title":"x"}))["dry_run"] and not calls
    assert not (await svc.update("/content/dam/a.jpg",{"dc:title":"x"},False,False))["success"]
    assert (await svc.update("/content/dam/a.jpg",{"dc:title":"x"},False,True))["success"]

@pytest.mark.asyncio
async def test_asset_publication_safety(monkeypatch):
    c=client(aem_write_enabled=True); calls=[]
    class S:
        async def replicate(self,*a): calls.append(a); return {}
    svc=PublicationService(c,S()); assert (await svc.asset("/content/dam/a.jpg","Deactivate"))["dry_run"] and not calls
    assert not (await svc.asset("/content/dam/a.jpg","Activate",False,False))["success"]
    await svc.asset("/content/dam/a.jpg","Activate",False,True); assert calls
