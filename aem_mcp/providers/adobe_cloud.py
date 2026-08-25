from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, TextContent

from ..adobe_cloud.errors import AdobeCloudAuthenticationError
from ..adobe_cloud.sessions import AdobeCloudSessionManager
from ..adobe_cloud import adobe_cloud_sessions
from ..aem_client import BinaryTooLargeError
from ..config import Settings
from ..services.asset_preview import IMAGE_MIME_TYPES, PDF_MIME_TYPE
from .errors import (
    AEMProviderAuthenticationRequired, AEMProviderConfigurationError,
    AEMProviderError, AEMProviderNotFound, AEMProviderPermissionDenied,
    AEMProviderRateLimited, AEMProviderRemoteError,
)


class AEMCloudApiRegistry:
    """Verified 2026 Page Management and Assets Author operation roots."""
    def __init__(self, settings: Settings) -> None:
        self.pages = settings.aem_cloud_pages_api_path.rstrip("/")
        self.assets = settings.aem_cloud_assets_api_path.rstrip("/")
    def page(self, suffix: str = "") -> str: return self.pages + suffix
    def asset(self, suffix: str = "") -> str: return self.assets + suffix


class AdobeCloudApiClient:
    def __init__(self, settings: Settings, sessions: AdobeCloudSessionManager, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings, self.sessions, self._client = settings, sessions, client
        self.origin = settings.aem_cloud_author_url.strip().rstrip("/")
        self.registry = AEMCloudApiRegistry(settings)
        if not self.origin:
            raise AEMProviderConfigurationError("AEM_CLOUD_AUTHOR_URL is required in cloud mode.")

    def _url(self, path: str) -> str:
        if not path.startswith("/") or "//" in path or any(part == ".." for part in path.split("/")):
            raise AEMProviderConfigurationError("Configured Adobe API path is invalid.")
        url = self.origin + path
        if urlsplit(url).netloc != urlsplit(self.origin).netloc:
            raise AEMProviderConfigurationError("Adobe API request host was rejected.")
        return url

    async def request(self, method: str, path: str, *, api_key: bool = False, json_body: Any = None, params: dict[str, Any] | None = None, accept: str = "application/json", stream: bool = False) -> httpx.Response:
        try:
            token = await self.sessions.get_valid_access_token()
        except AdobeCloudAuthenticationError as exc:
            raise AEMProviderAuthenticationRequired("Connect your Adobe account before using AEM Cloud tools.") from exc
        headers = {"Authorization": f"Bearer {token}", "Accept": accept}
        if api_key:
            headers["X-Api-Key"] = self.settings.adobe_cloud_client_id
        async def send(request_headers: dict[str, str]) -> httpx.Response:
            if self._client:
                return await self._client.request(method, self._url(path), headers=request_headers, json=json_body, params=params)
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
                return await client.request(method, self._url(path), headers=request_headers, json=json_body, params=params)
        try:
            response = await send(headers)
        except httpx.TimeoutException as exc:
            raise AEMProviderRemoteError("AEM Cloud request timed out.", code="aem_cloud_timeout") from exc
        except httpx.HTTPError as exc:
            raise AEMProviderRemoteError("AEM Cloud request failed.") from exc
        if 300 <= response.status_code < 400:
            raise AEMProviderRemoteError("AEM Cloud redirect was rejected.", code="aem_cloud_redirect_rejected")
        if response.status_code == 401 and hasattr(self.sessions, "store") and hasattr(self.sessions, "session_key"):
            # A token can be revoked before its advertised expiry. Force the
            # existing per-user refresh path once, then require reconnection.
            try:
                key = self.sessions.session_key()
                session = await self.sessions.store.get(key)
                if session and session.refresh_token:
                    session.expires_at = 0
                    await self.sessions.store.save(session)
                    refreshed = await self.sessions.get_valid_access_token(key)
                    retry_headers = dict(headers); retry_headers["Authorization"] = f"Bearer {refreshed}"
                    response = await send(retry_headers)
            except Exception:
                pass
        if response.status_code == 401:
            raise AEMProviderAuthenticationRequired("Adobe authentication is invalid or expired; reconnect is required.")
        if response.status_code == 403:
            raise AEMProviderPermissionDenied("Access was denied. The Adobe user permission, Developer Console API enablement, or client registration for this environment may be insufficient.")
        if response.status_code == 404: raise AEMProviderNotFound("The requested AEM Cloud resource or API route was not found.")
        if response.status_code == 429: raise AEMProviderRateLimited("AEM Cloud rate limit was reached; retry later.")
        if response.status_code >= 500: raise AEMProviderRemoteError("AEM Cloud service returned a server error.")
        if response.status_code >= 400: raise AEMProviderRemoteError("AEM Cloud rejected the request.")
        return response

    async def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.request(method, path, **kwargs)
        try: payload = response.json()
        except ValueError as exc: raise AEMProviderRemoteError("AEM Cloud returned malformed JSON.") from exc
        if not isinstance(payload, dict): raise AEMProviderRemoteError("AEM Cloud returned an unexpected response.")
        return payload


class PageResolver:
    def __init__(self, api: AdobeCloudApiClient) -> None: self.api = api
    async def resolve(self, author_path: str) -> dict[str, Any]:
        payload = await self.api.json("GET", self.api.registry.page(), params={"authorPath": author_path, "limit": 1})
        items = payload.get("items", payload.get("pages", []))
        if not isinstance(items, list) or not items or not isinstance(items[0], dict) or not items[0].get("id"):
            raise AEMProviderNotFound("The requested AEM page was not found.")
        return items[0]


class AssetResolver:
    def __init__(self, api: AdobeCloudApiClient) -> None: self.api = api
    async def resolve(self, path: str) -> dict[str, Any]:
        payload = await self.api.json("POST", self.api.registry.asset("/search"), json_body={"query": [{"term": {"repositoryMetadata.repo:path": [path]}}], "limit": 2})
        results = payload.get("hits", {}).get("results", []) if isinstance(payload.get("hits"), dict) else []
        exact = [item for item in results if isinstance(item, dict) and item.get("repositoryMetadata", {}).get("repo:path") == path]
        if len(exact) != 1 or not exact[0].get("assetId"): raise AEMProviderNotFound("The requested AEM asset was not found.")
        return exact[0]


class AdobeCloudProvider:
    def __init__(self, settings: Settings, *, sessions: AdobeCloudSessionManager | None = None, api: AdobeCloudApiClient | None = None) -> None:
        self.settings = settings; self.sessions = sessions or adobe_cloud_sessions
        self.api = api or AdobeCloudApiClient(settings, self.sessions)
        self.pages, self.assets = PageResolver(self.api), AssetResolver(self.api)

    @staticmethod
    def _safe_props(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict): return {}
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            if isinstance(key, str) and isinstance(item, (str, int, float, bool, type(None))): result[key] = item if not isinstance(item, str) or len(item) <= 10000 else item[:10000] + "...[truncated]"
            elif isinstance(key, str) and isinstance(item, list): result[key] = item[:100]
        return result

    def _path(self, path: str, *, dam: bool = False) -> str:
        if not path.startswith("/") or "//" in path or "\\" in path or any(x in {".", ".."} for x in path.split("/")) or any(x in path.lower() for x in ("%2e", "%2f", "%5c")):
            raise ValueError("AEM path is invalid.")
        normalized = path.rstrip("/") or "/"; roots = self.settings.dam_read_roots if dam else self.settings.allowed_roots
        if not roots or not any(normalized == root or normalized.startswith(root + "/") for root in roots): raise ValueError("AEM path is outside configured read roots.")
        return normalized

    async def get_page_properties(self, path: str) -> dict[str, Any]:
        path = self._path(path); page = await self.pages.resolve(path); page_id = quote(str(page["id"]), safe="")
        detail = await self.api.json("GET", self.api.registry.page("/" + page_id))
        metadata = detail.get("metadata", []); props = {str(x.get("name")): x.get("value") for x in metadata if isinstance(x, dict) and x.get("name")}
        for source, target in (("title", "jcr:title"), ("description", "jcr:description"), ("templateId", "cq:template"), ("tags", "cq:tags")):
            if source in detail: props[target] = detail[source]
        return {"path": path, "properties": self._safe_props(props)}

    async def list_child_pages(self, root: str, limit: int = 50) -> dict[str, Any]:
        root = self._path(root); parent = await self.pages.resolve(root); cap = min(max(limit, 1), 1000); items: list[Any] = []; cursor = None
        while len(items) < cap:
            params = {"parentPageId": parent["id"], "limit": min(50, cap-len(items))};
            if cursor: params["cursor"] = cursor
            payload = await self.api.json("GET", self.api.registry.page(), params=params); batch = payload.get("items", [])
            if not isinstance(batch, list): raise AEMProviderRemoteError("Page list response was malformed.")
            items.extend(batch); cursor = payload.get("cursor")
            if not cursor or not batch: break
        children = [{"path": p.get("authorPath"), "name": str(p.get("authorPath", "")).rsplit("/",1)[-1], "title": p.get("title")} for p in items[:cap] if isinstance(p, dict)]
        return {"root": root, "pages": children, "count": len(children), "limit": cap}

    async def find_component_usage(self, root: str, resource_type: str, limit: int = 50) -> dict[str, Any]:
        root = self._path(root); parent = await self.pages.resolve(root); cap=min(max(limit,1),1000); payload=await self.api.json("POST", self.api.registry.page("/search"), json_body={"componentType":resource_type,"matchMode":"exact","parentPageId":parent["id"],"limit":min(cap,50)})
        raw=payload.get("items",[]); hits=[]
        for page in raw if isinstance(raw,list) else []:
            if not isinstance(page,dict): continue
            page_path=page.get("authorPath"); locations=page.get("contentPaths",page.get("componentPaths",[]))
            for location in locations if isinstance(locations,list) else []:
                if len(hits)>=cap: break
                hits.append({"path":location,"pagePath":page_path,"resourceType":resource_type})
        return {"root":root,"resourceType":resource_type,"matches":hits,"count":len(hits),"limit":cap}

    async def _content(self, page_path: str) -> tuple[str, dict[str, Any]]:
        path=self._path(page_path); page=await self.pages.resolve(path); return path, await self.api.json("GET",self.api.registry.page("/"+quote(str(page["id"]),safe="")+"/content"))

    def _walk(self, page_path: str, tree: dict[str,Any], max_depth:int, limit:int) -> tuple[list[dict[str,Any]],set[str]]:
        found=[]; reasons=set(); content_root=page_path+"/jcr:content"
        def visit(node:dict[str,Any], parent:str, depth:int)->None:
            if len(found)>=limit: reasons.add("limit"); return
            ident=str(node.get("id") or node.get("capi-key") or len(found)); current=parent+"/"+ident
            ctype=node.get("componentType")
            if ctype: found.append({"path":current,"name":ident,"resourceType":ctype,"properties":self._safe_props(node.get("properties"))})
            items=node.get("items",[])
            if isinstance(items,list) and items:
                if depth>=max_depth: reasons.add("max_depth"); return
                for child in items:
                    if isinstance(child,dict): visit(child,current,depth+1)
        visit(tree,content_root,0); return found,reasons

    async def list_components(self,page_path:str,max_depth:int=10,limit:int=200)->dict[str,Any]:
        max_depth=min(max(max_depth,0),50); limit=min(max(limit,1),1000); path,tree=await self._content(page_path); items,reasons=self._walk(path,tree,max_depth,limit)
        return {"pagePath":path,"contentPath":path+"/jcr:content","components":items,"count":len(items),"maxDepth":max_depth,"limit":limit,"incomplete":bool(reasons),"incompleteReasons":sorted(reasons)}

    async def get_component_properties(self,component_path:str)->dict[str,Any]:
        marker="/jcr:content/"
        if marker not in component_path: raise ValueError("Component path must be below a page's jcr:content")
        page_path=component_path.split(marker,1)[0]; result=await self.list_components(page_path,50,1000)
        match=next((x for x in result["components"] if x["path"]==component_path),None)
        if not match: raise AEMProviderNotFound("The requested component was not found in Page Content.")
        return match

    async def _definition(self,page_path:str)->dict[str,Any]:
        path=self._path(page_path); page=await self.pages.resolve(path); return await self.api.json("GET",self.api.registry.page("/"+quote(str(page["id"]),safe="")+"/content/definition"))

    async def get_component_authoring_schema(self,resource_type:str)->dict[str,Any]:
        if not resource_type or resource_type.startswith("/") or ".." in resource_type.split("/"): raise ValueError("resource_type must be a safe relative resource type")
        # The Page Content Definition is page-specific. Find one permitted page using this component.
        search=await self.api.json("POST",self.api.registry.page("/search"),json_body={"componentType":resource_type,"matchMode":"exact","limit":1}); items=search.get("items",[])
        if not isinstance(items,list) or not items or not isinstance(items[0],dict) or not items[0].get("authorPath"): raise AEMProviderNotFound("No permitted page content definition exposes this component type.")
        definition=await self._definition(str(items[0]["authorPath"])); definitions=definition.get("componentDefinitions",[]); item=next((x for x in definitions if isinstance(x,dict) and x.get("componentType")==resource_type),None)
        if not item: raise AEMProviderNotFound("The component definition was not found.")
        fields=[]
        for field in item.get("fields",[]):
            if isinstance(field,dict): fields.append({"name":field.get("name"),"label":field.get("label") or field.get("name"),"field_type":field.get("component") or field.get("valueType"),"required":bool(field.get("required",False)),"source":"effective_cloud_definition","multifield":bool(field.get("multi",False)),**({"options":field["options"][:100]} if isinstance(field.get("options"),list) else {})})
        return {"resource_type":resource_type,"resource_super_type":item.get("componentSuperType"),"dialog_path":None,"fields":fields,"warnings":["Cloud output is the effective Page Content Definition; raw cq:dialog inheritance is not exposed."]}

    async def list_allowed_components(self,container_path:str,limit:int=200)->dict[str,Any]:
        marker="/jcr:content/"
        if marker not in container_path: raise ValueError("Container path must be below a page's jcr:content")
        page_path=container_path.split(marker,1)[0]
        _,tree=await self._content(page_path); walked,_=self._walk(page_path,tree,50,1000)
        container=next((item for item in walked if item["path"]==container_path),None)
        if not container: raise AEMProviderNotFound("The requested authored container was not found in Page Content.")
        definition=await self._definition(page_path); definitions=definition.get("componentDefinitions",[]); rules=definition.get("placementRules",[]); allowed=[]
        refs={str(item.get("id") or item.get("componentDefinitionRef") or item.get("componentType")):item for item in definitions if isinstance(item,dict)} if isinstance(definitions,list) else {}
        for rule in rules if isinstance(rules,list) else []:
            if not isinstance(rule,dict): continue
            target=rule.get("condition",{}).get("matchTarget",{}).get("componentDefinitionRef")
            targets=target if isinstance(target,list) else [target]
            if not any((refs.get(str(ref),{}).get("componentType") or ref)==container["resourceType"] for ref in targets): continue
            insertions=rule.get("action",{}).get("allowedInsertions",[])
            for insertion in insertions if isinstance(insertions,list) else []:
                if isinstance(insertion,dict) and insertion.get("componentDefinitionRef") is not None: allowed.append(str(insertion["componentDefinitionRef"]))
        unique=list(dict.fromkeys(allowed))[:min(max(limit,1),200)]; components=[]
        for ref in unique:
            item=refs.get(ref,{}); ctype=item.get("componentType") or ref
            components.append({"resource_type":ctype,"title":item.get("title"),"group":None})
        return {"container_path":container_path,"components":components,"warnings":[] if components else ["Page Content Definition did not expose a matching placement rule for this authored container path."]}

    async def _asset_search(self,root:str,text:str|None,mime_type:str|None,limit:int,offset:int)->tuple[list[dict[str,Any]],bool]:
        query=[{"match":{"text":root,"mode":"FULLTEXT","fields":["repositoryMetadata.repo:path"]}}]
        if text: query.append({"match":{"text":text,"mode":"FULLTEXT"}})
        if mime_type: query.append({"term":{"repositoryMetadata.dc:format":[mime_type]}})
        needed=offset+limit; collected=[]; cursor=None
        while len(collected)<needed:
            body={"query":query,"limit":min(50,needed-len(collected))};
            if cursor: body["cursor"]=cursor
            payload=await self.api.json("POST",self.api.registry.asset("/search"),json_body=body); hits=payload.get("hits",{}); batch=hits.get("results",[]) if isinstance(hits,dict) else []
            if not isinstance(batch,list): raise AEMProviderRemoteError("Asset search response was malformed.")
            # Enforce root locally as well; the remote API currently has no documented prefix operator.
            collected.extend(x for x in batch if isinstance(x,dict) and str(x.get("repositoryMetadata",{}).get("repo:path","")).startswith(root+"/"))
            cursor=payload.get("cursor") or (hits.get("cursor") if isinstance(hits,dict) else None)
            if not cursor or not batch: break
        return collected[offset:offset+limit], bool(cursor or len(collected)>offset+limit)

    async def search_assets(self,root:str="/content/dam",text:str|None=None,mime_type:str|None=None,limit:int=50,offset:int=0)->dict[str,Any]:
        root=self._path(root,dam=True); limit=min(max(limit,1),self.settings.aem_max_asset_search_limit); offset=max(offset,0); items,more=await self._asset_search(root,text,mime_type,limit,offset)
        assets=[]
        for item in items:
            repo=item.get("repositoryMetadata",{}); meta=item.get("assetMetadata",{}); assets.append({"path":repo.get("repo:path"),"name":repo.get("repo:name"),"mimeType":repo.get("dc:format"),"title":meta.get("dc:title")})
        return {"root":root,"assets":assets,"count":len(assets),"limit":limit,"offset":offset,"hasMore":more}

    async def get_asset_metadata(self,asset_path:str)->dict[str,Any]:
        path=self._path(asset_path,dam=True); hit=await self.assets.resolve(path); asset_id=quote(str(hit["assetId"]),safe=":")
        payload=await self.api.json("GET",self.api.registry.asset("/"+asset_id+"/metadata"),api_key=True); value=payload.get("value",payload); repo=value.get("repositoryMetadata",{}); meta=value.get("assetMetadata",{})
        safe_meta=self._safe_props(meta); mime=repo.get("dc:format") or meta.get("dc:format")
        return {"path":path,"name":repo.get("repo:name") or path.rsplit("/",1)[-1],"mime_type":mime,"size":repo.get("repo:size"),"width":meta.get("tiff:ImageWidth") or meta.get("tiff:imageWidth"),"height":meta.get("tiff:ImageLength") or meta.get("tiff:imageLength"),"metadata":safe_meta,"renditions":[]}

    async def get_asset_preview(self,asset_path:str,rendition:str|None=None,max_bytes:int|None=None)->CallToolResult:
        if rendition not in {None,"original"}: raise AEMProviderError("Named renditions are not exposed by the verified cloud binary operation.",code="cloud_rendition_not_supported")
        path=self._path(asset_path,dam=True); metadata=await self.get_asset_metadata(path); mime=str(metadata.get("mime_type") or "").lower(); allowed=set(self.settings.preview_allowed_mime_types)
        if mime not in allowed or mime not in IMAGE_MIME_TYPES|{PDF_MIME_TYPE}: raise ValueError("Asset MIME type is not previewable.")
        cap=min(max_bytes or self.settings.aem_max_preview_bytes,self.settings.aem_max_preview_bytes)
        hit=await self.assets.resolve(path); response=await self.api.request("GET",self.api.registry.asset("/"+quote(str(hit["assetId"]),safe=":")),api_key=True,accept=mime)
        declared=response.headers.get("content-length")
        if declared:
            try: declared_size=int(declared)
            except ValueError as exc: raise AEMProviderRemoteError("AEM Cloud binary returned an invalid Content-Length.") from exc
            if declared_size>cap: raise BinaryTooLargeError("AEM preview exceeds AEM_MAX_PREVIEW_BYTES")
        content=response.content
        if len(content)>cap: raise BinaryTooLargeError("AEM preview exceeds AEM_MAX_PREVIEW_BYTES")
        returned=response.headers.get("content-type","").split(";",1)[0].lower()
        if returned!=mime or returned not in allowed: raise ValueError("AEM asset binary returned an unexpected MIME type.")
        info={"asset_path":path,"mime_type":mime,"rendition_name":"web-optimized" if mime in IMAGE_MIME_TYPES else "original","width":metadata.get("width"),"height":metadata.get("height"),"content_length":len(content),"file_name":metadata.get("name"),"preview_type":"image" if mime in IMAGE_MIME_TYPES else "embedded_resource","warnings":["Cloud Assets API returns a web-optimized binary for images; named AEM renditions are not exposed."] if mime in IMAGE_MIME_TYPES else []}
        blocks:list[Any]=[TextContent(type="text",text=json.dumps(info,separators=(",",":")))]
        encoded=base64.b64encode(content).decode("ascii")
        if mime in IMAGE_MIME_TYPES: blocks.append(ImageContent(type="image",data=encoded,mimeType=mime))
        else: blocks.append(EmbeddedResource(type="resource",resource=BlobResourceContents(uri=f"aem-cloud-asset://{quote(path,safe='')}",mimeType=mime,blob=encoded)))
        return CallToolResult(content=blocks,structuredContent=info)
