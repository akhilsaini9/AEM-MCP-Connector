from __future__ import annotations
import asyncio
from typing import Any
from ..aem_client import AEMClient
from ..audit import audit_operation, confirmation_error, dry_run_result
from .dependencies import DependencyService


class LocalReplicationStrategy:
    def __init__(self, poll_attempts: int = 10, poll_interval_seconds: float = 0.2) -> None:
        self.poll_attempts = max(1, min(poll_attempts, 50))
        self.poll_interval_seconds = max(0.0, min(poll_interval_seconds, 2.0))

    @staticmethod
    def validate_result(payload: dict[str, Any]) -> dict[str, Any]:
        """Reject explicit servlet failures without requiring undocumented success keys."""
        if payload.get("success") is False:
            raise RuntimeError("AEM replication servlet reported failure")
        status = payload.get("status")
        if isinstance(status, str) and status.lower() in {"error", "failed", "failure"}:
            raise RuntimeError("AEM replication servlet reported failure")
        if payload.get("error"):
            raise RuntimeError("AEM replication servlet reported failure")
        return payload

    @staticmethod
    async def _replication_state(client: AEMClient, path: str) -> dict[str, Any]:
        try:
            node = await client._get_node_json(f"{path}/jcr:content")
        except ValueError:
            return {}
        return {key: node.get(key) for key in ("cq:lastReplicationAction", "cq:lastReplicated", "cq:lastReplicatedBy") if node.get(key) is not None}

    async def _verify_author_status(self, client: AEMClient, path: str, action: str, previous: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.poll_attempts):
            current = await self._replication_state(client, path)
            action_matches = current.get("cq:lastReplicationAction") == action
            timestamp = current.get("cq:lastReplicated")
            timestamp_changed = timestamp is not None and timestamp != previous.get("cq:lastReplicated")
            if action_matches and timestamp_changed:
                return current
            if attempt + 1 < self.poll_attempts and self.poll_interval_seconds:
                await asyncio.sleep(self.poll_interval_seconds)
        raise RuntimeError(f"AEM accepted replication but Author status did not verify action '{action}'")

    async def replicate(self, client: AEMClient, path: str, action: str) -> dict[str, Any]:
        previous = await self._replication_state(client, path)
        payload = await client.request_replication(path, action)
        self.validate_result(payload)
        verified = await self._verify_author_status(client, path, action, previous)
        return {"request_accepted":True,"author_status_verified":True,"delivery_to_publish_verified":False,"replication_action":verified.get("cq:lastReplicationAction"),"replicated_at":verified.get("cq:lastReplicated"),"replicated_by":verified.get("cq:lastReplicatedBy"),"servlet_response":payload}


class PublicationService:
    def __init__(self, client: AEMClient, strategy: LocalReplicationStrategy | None = None) -> None:
        self.client=client; self.strategy=strategy or LocalReplicationStrategy()

    async def page(self, page_path: str, action: str, include_references: bool=False, dry_run: bool=True, confirm: bool=False) -> dict[str, Any]:
        operation=("publish" if action=="Activate" else "unpublish")+"_page"
        path=self.client.validate_publish_path(page_path, require_write=not dry_run)
        deps=await DependencyService(self.client).get_page_dependencies(path)
        refs=deps["assets"]+deps["pages"]+deps["experience_fragments"]+deps["content_fragments"]
        targets=[path]+(refs if include_references and action=="Activate" else [])
        async with audit_operation(self.client.settings,operation,path,dry_run,confirm) as audit:
            if dry_run:
                audit.update(result="preview",success=True)
                return dry_run_result(operation,targets,[{"action":action,"path":p} for p in targets],deps["warnings"],page_path=path,would_publish=targets if action=="Activate" else [],references=refs)
            if not confirm:
                audit.update(result="rejected",success=False); return confirmation_error(operation,targets)
            for target in targets:
                if target.startswith("/content/dam/"):
                    self.client.validate_dam_write_path(target, require_write=True)
                else:
                    self.client.validate_publish_path(target, require_write=True)
            results=[await self.strategy.replicate(self.client,p,action) for p in targets]
            audit.update(result="success",success=True)
            return {"operation":operation,"dry_run":False,"affected_paths":targets,"planned_changes":[],"warnings":deps["warnings"],"requires_confirmation":False,"success":True,"results":results}

    async def asset(self, asset_path: str, action: str, dry_run: bool=True, confirm: bool=False, usages: list[dict[str,Any]]|None=None) -> dict[str,Any]:
        operation=("publish" if action=="Activate" else "unpublish")+"_asset"
        path=self.client.validate_dam_write_path(asset_path,require_write=not dry_run)
        self.client.validate_publish_path(path, require_write=not dry_run)
        warnings=[f"Asset has {len(usages)} known usage(s)"] if usages else []
        async with audit_operation(self.client.settings,operation,path,dry_run,confirm) as audit:
            if dry_run: audit.update(result="preview",success=True); return dry_run_result(operation,[path],[{"action":action,"path":path}],warnings,usages=usages or [])
            if not confirm: audit.update(result="rejected",success=False); return confirmation_error(operation,[path],warnings)
            result=await self.strategy.replicate(self.client,path,action); audit.update(result="success",success=True)
            return {"operation":operation,"dry_run":False,"affected_paths":[path],"planned_changes":[],"warnings":warnings,"requires_confirmation":False,"success":True,"aem_response":result}
