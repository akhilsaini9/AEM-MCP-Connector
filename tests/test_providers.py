from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aem_mcp.adobe_cloud.sessions import AdobeCloudSession, AdobeCloudSessionManager, MemoryAdobeCloudSessionStore
from aem_mcp.config import Settings
from aem_mcp.providers.adobe_cloud import AdobeCloudApiClient, AdobeCloudProvider
from aem_mcp.providers.errors import AEMProviderAuthenticationRequired, AEMProviderNotFound, AEMProviderPermissionDenied, AEMProviderRateLimited, AEMProviderRemoteError, AEMProviderUnsupported
from aem_mcp.providers.factory import get_aem_provider, require_local_runtime
from aem_mcp.providers.local import LocalAEMProvider


def cfg(**overrides: object) -> Settings:
    values = {"aem_runtime_mode":"cloud","adobe_cloud_enabled":True,"adobe_cloud_client_id":"client-id","adobe_cloud_client_secret":"secret","aem_cloud_author_url":"https://author-p1-e2.adobeaemcloud.com","aem_allowed_roots":"/content","aem_dam_read_roots":"/content/dam"}
    values.update(overrides); return Settings(_env_file=None, **values)


class Tokens:
    def __init__(self, token: str = "user-token") -> None: self.token = token
    async def get_valid_access_token(self) -> str:
        if not self.token: raise __import__("aem_mcp.adobe_cloud.errors",fromlist=["AdobeCloudAuthenticationError"]).AdobeCloudAuthenticationError()
        return self.token


class FakeHTTP:
    def __init__(self, responses: list[httpx.Response]) -> None: self.responses=responses; self.calls=[]
    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method,url,kwargs)); return self.responses.pop(0)


def res(status:int,data:Any=None,headers:dict[str,str]|None=None,content:bytes|None=None)->httpx.Response:
    request=httpx.Request("GET","https://author-p1-e2.adobeaemcloud.com/test")
    return httpx.Response(status,headers=headers,content=content,request=request) if content is not None else httpx.Response(status,json=data,headers=headers,request=request)


def test_runtime_default_factory_and_invalid_mode() -> None:
    assert Settings(_env_file=None).aem_runtime_mode == "local"
    assert isinstance(get_aem_provider(Settings(_env_file=None)), LocalAEMProvider)
    with pytest.raises(ValidationError): Settings(_env_file=None,aem_runtime_mode="invalid")


def test_cloud_factory_and_local_guard() -> None:
    from aem_mcp.providers.adobe_cloud import AdobeCloudProvider
    assert isinstance(get_aem_provider(cfg()),AdobeCloudProvider)
    with pytest.raises(AEMProviderUnsupported): require_local_runtime(cfg())


@pytest.mark.parametrize("url",["http://author.example","https://author.example/path","https://author.example?x=1","https://user@author.example"])
def test_author_origin_is_strict(url:str)->None:
    with pytest.raises(ValidationError): cfg(aem_cloud_author_url=url)


@pytest.mark.asyncio
async def test_auth_and_documented_headers_are_internal() -> None:
    fake=FakeHTTP([res(200,{"ok":True}),res(200,{"ok":True})]); api=AdobeCloudApiClient(cfg(),Tokens(),client=fake)  # type: ignore[arg-type]
    await api.json("GET","/adobe/sites/pages")
    await api.json("GET","/adobe/assets/id/metadata",api_key=True)
    assert fake.calls[0][2]["headers"] == {"Authorization":"Bearer user-token","Accept":"application/json"}
    assert fake.calls[1][2]["headers"]["X-Api-Key"] == "client-id"
    assert "secret" not in json.dumps(fake.calls)


@pytest.mark.asyncio
async def test_no_session_is_safe() -> None:
    api=AdobeCloudApiClient(cfg(),Tokens(""),client=FakeHTTP([]))  # type: ignore[arg-type]
    with pytest.raises(AEMProviderAuthenticationRequired) as caught: await api.json("GET","/adobe/sites/pages")
    assert "token" not in str(caught.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error",[(401,AEMProviderAuthenticationRequired),(403,AEMProviderPermissionDenied),(404,AEMProviderNotFound),(429,AEMProviderRateLimited),(500,AEMProviderRemoteError),(302,AEMProviderRemoteError)])
async def test_http_errors_are_normalized(status:int,error:type[Exception])->None:
    api=AdobeCloudApiClient(cfg(),Tokens(),client=FakeHTTP([res(status,{"access_token":"never expose"})]))  # type: ignore[arg-type]
    with pytest.raises(error) as caught: await api.json("GET","/adobe/sites/pages")
    assert "never expose" not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout_is_normalized() -> None:
    class TimeoutHTTP:
        async def request(self,*args:Any,**kwargs:Any)->httpx.Response: raise httpx.ReadTimeout("secret transport")
    api=AdobeCloudApiClient(cfg(),Tokens(),client=TimeoutHTTP())  # type: ignore[arg-type]
    with pytest.raises(AEMProviderRemoteError,match="timed out"): await api.json("GET","/adobe/sites/pages")


def provider_with(responses:list[httpx.Response])->tuple[AdobeCloudProvider,FakeHTTP]:
    fake=FakeHTTP(responses); api=AdobeCloudApiClient(cfg(),Tokens(),client=fake)  # type: ignore[arg-type]
    return AdobeCloudProvider(cfg(),sessions=Tokens(),api=api),fake  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_page_properties_and_children_pagination() -> None:
    provider,_=provider_with([res(200,{"items":[{"id":"p1","authorPath":"/content/site"}]}),res(200,{"title":"Home","metadata":[{"name":"navTitle","value":"Start"}]}),res(200,{"items":[{"id":"p1","authorPath":"/content/site"}]}),res(200,{"items":[{"authorPath":"/content/site/a","title":"A"}],"cursor":"next"}),res(200,{"items":[{"authorPath":"/content/site/b","title":"B"}]})])
    page=await provider.get_page_properties("/content/site"); assert page["properties"] == {"navTitle":"Start","jcr:title":"Home"}
    children=await provider.list_child_pages("/content/site",2); assert [x["path"] for x in children["pages"]]==["/content/site/a","/content/site/b"]


@pytest.mark.asyncio
async def test_component_traversal_depth_limit_and_lookup() -> None:
    tree={"id":"root","componentType":"root/type","properties":{},"items":[{"id":"text","componentType":"text/type","properties":{"text":"hello"},"items":[{"id":"deep","componentType":"deep/type","properties":{},"items":[]}]}]}
    provider,_=provider_with([res(200,{"items":[{"id":"p1"}]}),res(200,tree),res(200,{"items":[{"id":"p1"}]}),res(200,tree)])
    listed=await provider.list_components("/content/site",1,10); assert listed["incompleteReasons"]==["max_depth"]
    found=await provider.get_component_properties("/content/site/jcr:content/root/text"); assert found["properties"]["text"]=="hello"


@pytest.mark.asyncio
async def test_component_usage_schema_and_allowed_normalization() -> None:
    provider,_=provider_with([
        res(200,{"items":[{"id":"root"}]}),res(200,{"items":[{"authorPath":"/content/site/a","contentPaths":["/content/site/a/jcr:content/text"]}]}),
        res(200,{"items":[{"authorPath":"/content/site/a"}]}),res(200,{"items":[{"id":"a"}]}),res(200,{"componentDefinitions":[{"componentType":"text/type","componentSuperType":"base/type","fields":[{"name":"text","label":"Text","component":"text","required":True}]}]}),
        res(200,{"items":[{"id":"a"}]}),res(200,{"id":"root","componentType":"container/type","items":[]}),res(200,{"items":[{"id":"a"}]}),res(200,{"componentDefinitions":[{"id":"container","componentType":"container/type"},{"id":"text","componentType":"text/type","title":"Text"}],"placementRules":[{"condition":{"matchTarget":{"componentDefinitionRef":"container"}},"action":{"allowedInsertions":[{"componentDefinitionRef":"text"}]}}]})])
    usage=await provider.find_component_usage("/content/site","text/type",10); assert usage["count"]==1
    schema=await provider.get_component_authoring_schema("text/type"); assert schema["fields"][0]["required"] is True
    allowed=await provider.list_allowed_components("/content/site/a/jcr:content/root",10); assert allowed["components"][0]["resource_type"]=="text/type"


@pytest.mark.asyncio
async def test_asset_resolver_search_metadata_and_filters() -> None:
    hit={"assetId":"urn:aaid:aem:1","repositoryMetadata":{"repo:path":"/content/dam/site/a.jpg","repo:name":"a.jpg","dc:format":"image/jpeg","repo:size":5},"assetMetadata":{"dc:title":"A"}}
    provider,fake=provider_with([res(200,{"hits":{"results":[hit]}}),res(200,{"value":hit}),res(200,{"hits":{"results":[hit]}})])
    metadata=await provider.get_asset_metadata("/content/dam/site/a.jpg"); assert metadata["mime_type"]=="image/jpeg"
    search=await provider.search_assets("/content/dam/site","shoe","image/jpeg",10,0); assert search["assets"][0]["path"]=="/content/dam/site/a.jpg"
    body=fake.calls[-1][2]["json"]; assert body["query"][1]["match"]["text"]=="shoe" and body["query"][2]["term"]["repositoryMetadata.dc:format"]==["image/jpeg"]


@pytest.mark.asyncio
async def test_preview_image_contract_mime_size_and_no_url() -> None:
    hit={"assetId":"urn:aaid:aem:1","repositoryMetadata":{"repo:path":"/content/dam/a.jpg","repo:name":"a.jpg","dc:format":"image/jpeg"},"assetMetadata":{}}
    provider,_=provider_with([res(200,{"hits":{"results":[hit]}}),res(200,{"value":hit}),res(200,{"hits":{"results":[hit]}}),res(200,content=b"jpeg",headers={"content-type":"image/jpeg"})])
    result=await provider.get_asset_preview("/content/dam/a.jpg"); assert result.structured_content["content_length"]==4 and "https://" not in str(result.structured_content)
    provider,_=provider_with([res(200,{"hits":{"results":[hit]}}),res(200,{"value":hit}),res(200,{"hits":{"results":[hit]}}),res(200,content=b"oversize",headers={"content-type":"image/jpeg"})])
    with pytest.raises(Exception,match="exceeds"): await provider.get_asset_preview("/content/dam/a.jpg",max_bytes=2)


@pytest.mark.asyncio
async def test_two_users_use_separate_tokens(monkeypatch:pytest.MonkeyPatch)->None:
    store=MemoryAdobeCloudSessionStore(); await store.save(AdobeCloudSession("a",access_token="token-a",expires_at=time.time()+100)); await store.save(AdobeCloudSession("b",access_token="token-b",expires_at=time.time()+100))
    manager=AdobeCloudSessionManager(cfg(),store=store); key="a"; manager.session_key=lambda:key  # type: ignore[method-assign]
    fake=FakeHTTP([res(200,{"ok":True}),res(200,{"ok":True})]); api=AdobeCloudApiClient(cfg(),manager,client=fake)
    await api.json("GET","/adobe/sites/pages"); key="b"; await api.json("GET","/adobe/sites/pages")
    assert [x[2]["headers"]["Authorization"] for x in fake.calls]==["Bearer token-a","Bearer token-b"]


@pytest.mark.asyncio
async def test_api_401_forces_existing_per_user_refresh_once() -> None:
    class RefreshOAuth:
        async def refresh(self, _: str):
            from aem_mcp.adobe_cloud.auth import AdobeTokenResponse
            return AdobeTokenResponse("refreshed-token",100,"refresh")
    store=MemoryAdobeCloudSessionStore(); await store.save(AdobeCloudSession("a",access_token="old-token",refresh_token="refresh",expires_at=time.time()+100))
    manager=AdobeCloudSessionManager(cfg(),store=store,oauth=RefreshOAuth()); manager.session_key=lambda:"a"  # type: ignore[method-assign]
    fake=FakeHTTP([res(401,{}),res(200,{"ok":True})]); api=AdobeCloudApiClient(cfg(),manager,client=fake)
    assert (await api.json("GET","/adobe/sites/pages"))["ok"] is True
    assert fake.calls[1][2]["headers"]["Authorization"]=="Bearer refreshed-token"


@pytest.mark.asyncio
async def test_non_migrated_search_pages_never_hits_local_in_cloud_mode(monkeypatch:pytest.MonkeyPatch) -> None:
    from aem_mcp import server
    called=False
    async def local_call(*_:Any,**__:Any)->dict[str,Any]:
        nonlocal called; called=True; return {}
    monkeypatch.setattr("aem_mcp.providers.factory.get_settings",lambda:cfg())
    monkeypatch.setattr("aem_mcp.aem_client.AEMClient.search_pages",local_call)
    with pytest.raises(AEMProviderUnsupported): await server.search_pages("/content")
    assert called is False
