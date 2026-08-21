from __future__ import annotations
from typing import Any
import pytest
from aem_mcp.aem_client import AEMClient
from aem_mcp.config import Settings
from aem_mcp.services.publication import LocalReplicationStrategy

def client_with_states(monkeypatch:pytest.MonkeyPatch,states:list[dict[str,Any]],servlet_response:dict[str,Any]|None=None)->AEMClient:
    client=AEMClient();client.settings=Settings(_env_file=None);remaining=list(states)
    async def get_node(path:str)->dict[str,Any]:
        assert path.endswith("/jcr:content");return remaining.pop(0) if len(remaining)>1 else remaining[0]
    async def replicate(path:str,action:str)->dict[str,Any]:return dict(servlet_response or {})
    monkeypatch.setattr(client,"_get_node_json",get_node);monkeypatch.setattr(client,"request_replication",replicate);return client

@pytest.mark.asyncio
async def test_empty_payload_verified_by_changed_author_status(monkeypatch):
    client=client_with_states(monkeypatch,[{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"before"},{"cq:lastReplicationAction":"Activate","cq:lastReplicated":"after","cq:lastReplicatedBy":"admin"}])
    result=await LocalReplicationStrategy(1,0).replicate(client,"/content/site/page","Activate")
    assert result=={"request_accepted":True,"author_status_verified":True,"delivery_to_publish_verified":False,"replication_action":"Activate","replicated_at":"after","replicated_by":"admin","servlet_response":{}}

@pytest.mark.asyncio
async def test_verification_polls_until_metadata_changes(monkeypatch):
    client=client_with_states(monkeypatch,[{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"before"},{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"before"},{"cq:lastReplicationAction":"Activate","cq:lastReplicated":"after"}])
    result=await LocalReplicationStrategy(2,0).replicate(client,"/content/dam/a.jpg","Activate")
    assert result["author_status_verified"] is True

@pytest.mark.asyncio
@pytest.mark.parametrize("post_state",[{"cq:lastReplicationAction":"Activate","cq:lastReplicated":"same"},{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"new"},{}])
async def test_stale_wrong_or_missing_status_is_not_success(monkeypatch,post_state):
    client=client_with_states(monkeypatch,[{"cq:lastReplicationAction":"Activate","cq:lastReplicated":"same"},post_state])
    with pytest.raises(RuntimeError,match="did not verify"):
        await LocalReplicationStrategy(1,0).replicate(client,"/content/site/page","Activate")

@pytest.mark.asyncio
async def test_deactivation_verification(monkeypatch):
    client=client_with_states(monkeypatch,[{"cq:lastReplicationAction":"Activate","cq:lastReplicated":"before"},{"cq:lastReplicationAction":"Deactivate","cq:lastReplicated":"after"}],{"status":"OK"})
    result=await LocalReplicationStrategy(1,0).replicate(client,"/content/site/page","Deactivate")
    assert result["replication_action"]=="Deactivate"

@pytest.mark.parametrize("payload",[{"success":False},{"status":"failure"},{"error":"denied"}])
def test_explicit_servlet_failures_remain_rejected(payload):
    with pytest.raises(RuntimeError,match="reported failure"):LocalReplicationStrategy.validate_result(payload)
