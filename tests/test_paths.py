import pytest
from aem_mcp.aem_client import AEMClient

def test_content_path_allowed():
    client = AEMClient()
    assert client._validate_read_path("/content/wknd/us/en") == "/content/wknd/us/en"

def test_traversal_rejected():
    client = AEMClient()
    with pytest.raises(ValueError):
        client._validate_read_path("/content/../home")

def test_system_path_rejected():
    client = AEMClient()
    with pytest.raises(ValueError):
        client._validate_read_path("/system/console")

def test_write_disabled_by_default():
    client = AEMClient()
    if not client.settings.aem_write_enabled:
        with pytest.raises(PermissionError):
            client._validate_write_path("/content/mcp-poc/test")
