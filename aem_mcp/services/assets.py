from __future__ import annotations
import re
from urllib.parse import quote, unquote
from typing import Any
from ..aem_client import AEMClient
from ..audit import audit_operation, confirmation_error, dry_run_result

SAFE_METADATA = {"dc:title", "dc:description", "dc:subject", "cq:tags", "xmp:Title", "xmp:Description"}


def dam_path_to_assets_api_path(path: str) -> str:
    """Map a normalized repository DAM folder to its Assets HTTP API path."""
    if not isinstance(path, str) or not path.startswith("/") or "//" in path:
        raise ValueError("DAM path must be a normalized absolute path")
    decoded = unquote(path)
    lowered = path.lower()
    if any(token in lowered for token in ("%2f", "%5c", "%2e")):
        raise ValueError("Encoded DAM path traversal or separators are not allowed")
    segments = decoded.rstrip("/").split("/")
    if any(segment in {"", ".", ".."} for segment in segments[1:]):
        raise ValueError("DAM path traversal is not allowed")
    normalized = decoded.rstrip("/") or "/"
    if normalized != "/content/dam" and not normalized.startswith("/content/dam/"):
        raise ValueError("Assets API paths must be below /content/dam")
    relative = normalized.removeprefix("/content/dam").strip("/")
    if not relative:
        return "/api/assets"
    return "/api/assets/" + "/".join(quote(segment, safe="") for segment in relative.split("/"))


def _hit_value(hit: dict[str, Any], *names: str) -> Any:
    for name in names:
        value=hit.get(name)
        if value is not None: return value
    return None


class LocalAssetUploadStrategy:
    async def upload(self, client: AEMClient, folder: str, name: str, content: bytes, mime_type: str, *, overwrite: bool = False) -> dict[str, Any]:
        destination = f"{folder.rstrip('/')}/{name}"
        return await client.put_asset_binary(
            dam_path_to_assets_api_path(destination),
            content,
            mime_type,
            overwrite=overwrite,
        )


class AssetService:
    def __init__(self, client: AEMClient, upload_strategy: LocalAssetUploadStrategy|None=None) -> None:
        self.client=client; self.upload_strategy=upload_strategy or LocalAssetUploadStrategy()

    @staticmethod
    def _validate_metadata_properties(properties: dict[str,Any]) -> None:
        if not properties: raise ValueError("At least one metadata property is required")
        for name,value in properties.items():
            if name not in SAFE_METADATA or name.startswith("jcr:") or name.startswith("sling:") or name.startswith(":") or "/" in name: raise ValueError(f"Unsafe asset metadata property: {name}")
            if isinstance(value,(dict,bytes,bytearray)) or (isinstance(value,list) and any(isinstance(v,(dict,list,bytes,bytearray)) for v in value)): raise ValueError(f"Metadata value must be scalar or flat list: {name}")

    async def search(self, root: str="/content/dam", text: str|None=None, mime_type: str|None=None, limit: int=50, offset: int=0) -> dict[str,Any]:
        root=self.client.validate_dam_read_path(root); limit=min(max(limit,1),self.client.settings.aem_max_asset_search_limit); offset=max(offset,0)
        params:dict[str,str|int]={"path":root,"type":"dam:Asset","p.limit":limit,"p.offset":offset,"p.guessTotal":"true","orderby":"path","p.hits":"selective","p.properties":"jcr:path jcr:content/metadata/dc:title jcr:content/metadata/dc:description jcr:content/metadata/dc:format jcr:content/metadata/tiff:ImageWidth jcr:content/metadata/tiff:ImageLength jcr:content/metadata/cq:tags jcr:content/renditions/original/jcr:content/jcr:data"}
        if text: params.update({"fulltext":text,"fulltext.relPath":"jcr:content/metadata"})
        if mime_type: params.update({"property":"jcr:content/metadata/dc:format","property.value":mime_type})
        raw=await self.client._querybuilder(params); items=[]
        for hit in raw["hits"]:
            path=hit.get("path") or hit.get("jcr:path"); items.append({"path":path,"name":path.rsplit("/",1)[-1] if path else None,"title":_hit_value(hit,"jcr:content/metadata/dc:title","dc:title"),"description":_hit_value(hit,"jcr:content/metadata/dc:description","dc:description"),"mime_type":_hit_value(hit,"jcr:content/metadata/dc:format","dc:format"),"width":_hit_value(hit,"jcr:content/metadata/tiff:ImageWidth","tiff:ImageWidth"),"height":_hit_value(hit,"jcr:content/metadata/tiff:ImageLength","tiff:ImageLength"),"file_size":None,"tags":_hit_value(hit,"jcr:content/metadata/cq:tags","cq:tags") or []})
        return {"root":root,"assets":items,"count":len(items),"limit":limit,"offset":offset,"more":raw.get("more",False)}

    async def metadata(self, asset_path: str) -> dict[str,Any]:
        path=self.client.validate_dam_read_path(asset_path); asset=await self.client._get_node_json(path); metadata={}
        try: metadata=await self.client._get_node_json(path+"/jcr:content/metadata")
        except ValueError: pass
        safe=self.client._safe_properties(metadata)
        renditions=[]
        try:
            tree=await self.client._get_node_json(path+"/jcr:content/renditions")
            renditions=[{"name":name,"path":path+"/jcr:content/renditions/"+name} for name,value in tree.items() if isinstance(value,dict)][:100]
        except ValueError: pass
        return {"path":path,"name":path.rsplit("/",1)[-1],"title":safe.get("dc:title"),"description":safe.get("dc:description"),"mime_type":safe.get("dc:format"),"file_size":safe.get("dam:size"),"width":safe.get("tiff:ImageWidth"),"height":safe.get("tiff:ImageLength"),"tags":safe.get("cq:tags",[]),"creation_date":asset.get("jcr:created"),"modification_date":asset.get("jcr:lastModified") or safe.get("xmp:ModifyDate"),"metadata":safe,"renditions":renditions}

    async def usage(self, asset_path: str, root: str|None=None, limit: int=100) -> dict[str,Any]:
        asset=self.client.validate_dam_read_path(asset_path); root=self.client._validate_read_path(root or "/content"); limit=min(max(limit,1),self.client.settings.aem_max_asset_usage_limit)
        raw=await self.client._querybuilder({"path":root,"property":"fileReference","property.value":asset,"p.limit":limit,"p.guessTotal":"true","orderby":"path","p.hits":"selective","p.properties":"jcr:path fileReference"})
        usages=[]
        for hit in raw["hits"]:
            cp=hit.get("path") or hit.get("jcr:path") or ""; page=cp.split("/jcr:content",1)[0] if "/jcr:content" in cp else None
            usages.append({"page_path":page,"component_path":cp,"property_name":"fileReference"})
        return {"asset_path":asset,"usage_count":len(usages),"usages":usages,"limit":limit,"more":raw.get("more",False)}

    async def upload(self, dam_folder:str, file_name:str, content_base64:str, mime_type:str|None=None, metadata:dict[str,Any]|None=None, overwrite:bool=False, dry_run:bool=True, confirm:bool=False)->dict[str,Any]:
        folder=self.client.validate_dam_write_path(dam_folder,require_write=not dry_run)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]*",file_name or "") or file_name in {".",".."}: raise ValueError("file_name must be a safe single file name")
        max_bytes = self.client.settings.aem_max_asset_upload_bytes
        max_encoded_length = 4 * ((max_bytes + 2) // 3)
        if len(content_base64) > max_encoded_length:
            raise ValueError("Encoded asset exceeds AEM_MAX_ASSET_UPLOAD_BYTES")
        content=self.client.decode_base64_content(content_base64); mime=(mime_type or "application/octet-stream").lower()
        if metadata is not None: self._validate_metadata_properties(metadata)
        if len(content)>max_bytes: raise ValueError("Asset exceeds AEM_MAX_ASSET_UPLOAD_BYTES")
        if mime not in self.client.settings.allowed_asset_mime_types: raise ValueError("Asset MIME type is not allowed")
        destination=folder+"/"+file_name; exists=await self.client.node_exists(destination)
        if exists and not overwrite: raise FileExistsError(f"Asset already exists: {destination}")
        changes=[{"action":"upload","destination":destination,"mime_type":mime,"size":len(content),"overwrite":overwrite}]
        async with audit_operation(self.client.settings,"upload_asset",destination,dry_run,confirm) as audit:
            if dry_run: audit.update(result="preview",success=True); return dry_run_result("upload_asset",[destination],changes,[],destination=destination,exists=exists,mime_type=mime,size=len(content))
            if not confirm: audit.update(result="rejected",success=False); return confirmation_error("upload_asset",[destination])
            response=await self.upload_strategy.upload(self.client,folder,file_name,content,mime,overwrite=exists and overwrite)
            if metadata: await self.update(destination,metadata,False,True)
            audit.update(result="success",success=True); return {"operation":"upload_asset","dry_run":False,"affected_paths":[destination],"planned_changes":[],"warnings":[],"requires_confirmation":False,"success":True,"aem_response":response}

    async def update(self, asset_path:str, properties:dict[str,Any], dry_run:bool=True, confirm:bool=False)->dict[str,Any]:
        path=self.client.validate_dam_write_path(asset_path,require_write=not dry_run)
        self._validate_metadata_properties(properties)
        usages=(await self.usage(path,limit=100))["usages"]
        changes=[{"property":k,"value":v} for k,v in properties.items()]
        async with audit_operation(self.client.settings,"update_asset_metadata",path,dry_run,confirm) as audit:
            if dry_run: audit.update(result="preview",success=True); return dry_run_result("update_asset_metadata",[path],changes,[],usages=usages)
            if not confirm: audit.update(result="rejected",success=False); return confirmation_error("update_asset_metadata",[path])
            response=await self.client.post_form_unchecked(path+"/jcr:content/metadata",properties); audit.update(result="success",success=True)
            return {"operation":"update_asset_metadata","dry_run":False,"affected_paths":[path],"planned_changes":[],"warnings":[],"requires_confirmation":False,"success":True,"changed_properties":list(properties),"aem_response":response}
