from __future__ import annotations
from typing import Any
from ..aem_client import AEMClient

FIELD_TYPES={"textfield","textarea","pathfield","checkbox","select","numberfield","datepicker","hidden","fileupload","radiogroup","autocomplete","tags"}
STRUCTURAL_TYPES={"container","tabs","well","fixedcolumns","fieldset","accordion","panel","datasource","validation"}


class AuthoringService:
    def __init__(self,client:AEMClient)->None: self.client=client

    @staticmethod
    def _resource_path(resource_type:str)->str:
        if not resource_type or resource_type.startswith("/") or any(x in {".",".."} for x in resource_type.split("/")): raise ValueError("resource_type must be a safe relative resource type")
        return "/apps/"+resource_type

    async def _node_optional(self,path:str)->dict[str,Any]|None:
        try:return await self.client._get_node_json(path)
        except ValueError as exc:
            if "not found" in str(exc): return None
            raise

    async def _resolve_component(self,resource_type:str)->tuple[list[tuple[str,dict[str,Any]]],list[str]]:
        depth=max(1,min(self.client.settings.aem_component_dialog_max_inheritance_depth,50)); chain=[]; warnings=[]; seen=set(); current=resource_type
        for _ in range(depth):
            if current in seen: warnings.append("Resource-super-type inheritance loop detected"); break
            seen.add(current); path=self._resource_path(current); node=await self._node_optional(path)
            if node is None:
                path="/libs/"+current; node=await self._node_optional(path)
            if node is None: warnings.append(f"Component definition not found: {current}"); break
            chain.append((path,node)); super_type=node.get("sling:resourceSuperType")
            if not isinstance(super_type,str) or not super_type: break
            current=super_type
        else: warnings.append("Maximum inheritance depth reached")
        return chain,warnings

    async def definition(self,resource_type:str)->dict[str,Any]:
        chain,warnings=await self._resolve_component(resource_type)
        if not chain: return {"resource_type":resource_type,"path":None,"title":None,"component_group":None,"resource_super_type":None,"inheritance_chain":[],"has_dialog":False,"dialog_path":None,"warnings":warnings}
        path,node=chain[0]; dialog=path+"/cq:dialog"; has=await self._node_optional(dialog) is not None
        return {"resource_type":resource_type,"path":path,"title":node.get("jcr:title"),"component_group":node.get("componentGroup"),"resource_super_type":node.get("sling:resourceSuperType"),"inheritance_chain":[p.removeprefix("/apps/").removeprefix("/libs/") for p,_ in chain],"has_dialog":has,"dialog_path":dialog if has else None,"warnings":warnings}

    async def schema(self,resource_type:str)->dict[str,Any]:
        chain,warnings=await self._resolve_component(resource_type); fields_by_name={}; dialog_path=None
        for index,(path,_) in reversed(list(enumerate(chain))):
            dp=path+"/cq:dialog"; dialog=await self._node_optional(dp)
            if dialog is None: continue
            if index==0: dialog_path=dp
            async def visit(parent:str,tree:dict[str,Any],depth:int=0)->None:
                if depth>20: warnings.append(f"Dialog traversal depth exceeded at {parent}"); return
                for name,child in tree.items():
                    if not isinstance(child,dict): continue
                    cp=parent+"/"+name; rt=str(child.get("sling:resourceType","")).rsplit("/",1)[-1]; prop=child.get("name")
                    if isinstance(prop,str) and prop.startswith("./"): prop=prop[2:]
                    subtree=await self._node_optional(cp)
                    combined = dict(child)
                    if subtree: combined.update(subtree)
                    if rt == "multifield":
                        child_fields=[]
                        async def collect_fields(node:dict[str,Any],node_path:str)->None:
                            for child_name,nested in node.items():
                                if not isinstance(nested,dict): continue
                                nested_path=node_path+"/"+child_name
                                nested_rt=str(nested.get("sling:resourceType","")).rsplit("/",1)[-1]
                                nested_prop=nested.get("name")
                                if isinstance(nested_prop,str) and nested_prop.startswith("./"): nested_prop=nested_prop[2:]
                                if nested_rt in FIELD_TYPES and isinstance(nested_prop,str) and nested_prop:
                                    child_fields.append({"name":nested_prop,"label":nested.get("fieldLabel") or nested.get("jcr:title") or nested_prop,"field_type":nested_rt,"required":bool(nested.get("required",False)),"dialog_node_path":nested_path})
                                else:
                                    await collect_fields(nested,nested_path)
                        await collect_fields(combined,cp)
                        composite=bool(child.get("composite",False)) or any(str(v.get("sling:resourceType","")).endswith("/container") for v in combined.values() if isinstance(v,dict))
                        multifield_name=prop
                        if not multifield_name and not composite and len(child_fields)==1: multifield_name=child_fields[0]["name"]
                        if isinstance(multifield_name,str) and multifield_name:
                            fields_by_name[multifield_name]={"name":multifield_name,"label":child.get("fieldLabel") or child.get("jcr:title") or multifield_name,"field_type":"multifield","required":bool(child.get("required",False)),"source":"local" if index==0 else "inherited","multifield":True,"composite":composite,"child_fields":child_fields,"dialog_node_path":cp}
                        else: warnings.append(f"Multifield at {cp} has no reliable persisted property name")
                        continue
                    if isinstance(prop,str) and prop and rt in FIELD_TYPES:
                        fields_by_name[prop]={"name":prop,"label":child.get("fieldLabel") or child.get("jcr:title") or prop,"field_type":rt,"required":bool(child.get("required",False)),"source":"local" if index==0 else "inherited","multifield":False,"dialog_node_path":cp}
                    elif isinstance(prop,str) and prop and rt and rt not in STRUCTURAL_TYPES:
                        warnings.append(f"Unsupported dialog field resource type '{child.get('sling:resourceType')}' (type '{rt}') at {cp}")
                    if combined and any(isinstance(v,dict) for v in combined.values()): await visit(cp,combined,depth+1)
            await visit(dp,dialog)
        return {"resource_type":resource_type,"resource_super_type":chain[0][1].get("sling:resourceSuperType") if chain else None,"dialog_path":dialog_path,"fields":list(fields_by_name.values()),"warnings":list(dict.fromkeys(warnings))}

    async def allowed_components(self,container_path:str,limit:int=200)->dict[str,Any]:
        container=self.client._validate_component_path(container_path); limit=min(max(limit,1),200); node=await self.client._get_node_json(container); warnings=[]
        policy_path=node.get("cq:policy") or node.get("policyPath")
        if not isinstance(policy_path,str):
            warnings.append("Exact content policy could not be resolved from the container")
            return {"container_path":container,"components":[],"warnings":warnings}
        policy=await self._node_optional(policy_path)
        if policy is None: return {"container_path":container,"components":[],"warnings":[f"Content policy not found: {policy_path}"]}
        allowed=policy.get("components") or policy.get("allowedComponents") or []
        if isinstance(allowed,str): allowed=[allowed]
        components=[]
        for rt in allowed[:limit]:
            if not isinstance(rt,str): continue
            definition=await self.definition(rt); components.append({"resource_type":rt,"title":definition["title"],"group":definition["component_group"]})
        if len(allowed)>limit: warnings.append("Allowed component results were limited")
        if not components: warnings.append("Policy did not expose an explicit allowed-components list")
        return {"container_path":container,"components":components,"warnings":warnings}
