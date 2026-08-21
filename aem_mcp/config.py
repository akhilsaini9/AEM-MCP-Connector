from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    aem_base_url: str = "http://localhost:4502"
    aem_username: str = "admin"
    aem_password: str = "admin"
    aem_allowed_roots: str = "/content,/conf"
    aem_write_enabled: bool = False
    aem_write_roots: str = "/content/mcp-poc"
    aem_component_allowed_resource_types: str = ""
    aem_publish_allowed_roots: str = ""
    aem_dam_read_roots: str = "/content/dam"
    aem_dam_write_roots: str = "/content/dam"
    aem_max_asset_search_limit: int = 200
    aem_max_asset_usage_limit: int = 500
    aem_max_asset_upload_bytes: int = 26_214_400
    aem_allowed_asset_mime_types: str = "image/jpeg,image/png,image/webp,image/gif,application/pdf"
    aem_preview_allowed_mime_types: str = "image/jpeg,image/png,image/webp,image/gif,application/pdf"
    aem_max_preview_bytes: int = 5_242_880
    aem_component_dialog_max_inheritance_depth: int = 10
    aem_timeout_seconds: float = 20
    aem_verify_ssl: bool = False

    # MCP transport settings. Stdio remains the default for backward compatibility.
    mcp_transport: str = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    mcp_http_auth_enabled: bool = True
    mcp_http_bearer_token: str = ""
    mcp_http_allowed_hosts: str = "127.0.0.1:*,localhost:*"
    mcp_http_allowed_origins: str = "http://127.0.0.1:*,http://localhost:*"
    mcp_http_max_body_bytes: int = 1_048_576
    mcp_http_log_level: str = "INFO"
    mcp_audit_log_enabled: bool = True
    mcp_audit_log_level: str = "INFO"

    # Optional downstream Adobe-hosted AEM MCP integration. Disabled by default
    # so Local AEM behavior and startup remain unchanged.
    adobe_mcp_enabled: bool = False
    adobe_mcp_server_url: str = "https://mcp.adobeaemcloud.com/adobe/mcp/cloudmanager"
    adobe_mcp_allowed_tools: str = ""
    adobe_mcp_session_store: str = "memory"
    adobe_mcp_oauth_redirect_uri: str = ""
    adobe_mcp_single_developer_mode: bool = False
    adobe_mcp_environments_tool: str = ""
    adobe_mcp_connect_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @staticmethod
    def _roots(raw: str) -> tuple[str, ...]:
        return tuple(
            value.strip().rstrip("/")
            for value in raw.split(",")
            if value.strip()
        )

    @property
    def allowed_roots(self) -> tuple[str, ...]:
        return self._roots(self.aem_allowed_roots)

    @property
    def write_roots(self) -> tuple[str, ...]:
        return self._roots(self.aem_write_roots)

    @property
    def component_allowed_resource_types(self) -> tuple[str, ...]:
        return self._roots(self.aem_component_allowed_resource_types)

    @property
    def publish_allowed_roots(self) -> tuple[str, ...]:
        return self._roots(self.aem_publish_allowed_roots) or self.write_roots

    @property
    def dam_read_roots(self) -> tuple[str, ...]:
        return self._roots(self.aem_dam_read_roots)

    @property
    def dam_write_roots(self) -> tuple[str, ...]:
        return self._roots(self.aem_dam_write_roots)

    @property
    def allowed_asset_mime_types(self) -> tuple[str, ...]:
        return tuple(value.strip().lower() for value in self.aem_allowed_asset_mime_types.split(",") if value.strip())

    @property
    def preview_allowed_mime_types(self) -> tuple[str, ...]:
        return tuple(value.strip().lower() for value in self.aem_preview_allowed_mime_types.split(",") if value.strip())

    @property
    def http_allowed_hosts(self) -> list[str]:
        return list(self._roots(self.mcp_http_allowed_hosts))

    @property
    def http_allowed_origins(self) -> list[str]:
        return [value.strip() for value in self.mcp_http_allowed_origins.split(",") if value.strip()]

    @property
    def adobe_mcp_allowed_tool_names(self) -> frozenset[str]:
        return frozenset(
            value.strip() for value in self.adobe_mcp_allowed_tools.split(",") if value.strip()
        )

@lru_cache
def get_settings() -> Settings:
    return Settings()
