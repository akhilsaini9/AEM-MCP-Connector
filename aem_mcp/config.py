from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from urllib.parse import urlsplit

class Settings(BaseSettings):
    aem_runtime_mode: str = "local"
    aem_base_url: str = "http://localhost:4502"
    aem_username: str = "admin"
    aem_password: str = "admin"
    aem_allowed_roots: str = "/content,/conf"
    aem_write_enabled: bool = False
    aem_write_roots: str = "/content/mcp-poc"
    aem_component_allowed_resource_types: str = ""
    aem_publish_allowed_roots: str = ""
    aem_package_allowed_roots: str = "/content,/content/dam,/conf"
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
    # In explicit mode this supersedes MCP_HTTP_AUTH_ENABLED. An empty value
    # preserves the legacy enabled=true -> static_bearer mapping.
    mcp_auth_mode: str = ""
    mcp_oauth_enabled: bool = False
    mcp_oauth_issuer: str = "https://accounts.google.com"
    mcp_oauth_client_id: str = ""
    mcp_oauth_client_secret: str = ""
    mcp_oauth_required_scopes: str = "openid,email,profile"
    mcp_oauth_audience: str = ""
    mcp_public_base_url: str = "http://127.0.0.1:8000"
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

    # Direct Adobe IMS OAuth Web App integration. This is deliberately separate
    # from the downstream Adobe-hosted MCP integration above.
    adobe_cloud_enabled: bool = False
    adobe_cloud_client_id: str = ""
    adobe_cloud_client_secret: str = ""
    adobe_cloud_redirect_uri: str = "https://aem-mcp-connector.onrender.com/adobe-cloud/oauth/callback"
    adobe_cloud_authorization_endpoint: str = "https://ims-na1.adobelogin.com/ims/authorize/v2"
    adobe_cloud_token_endpoint: str = "https://ims-na1.adobelogin.com/ims/token/v3"
    adobe_cloud_userinfo_endpoint: str = "https://ims-na1.adobelogin.com/ims/userinfo/v2"
    adobe_cloud_scopes: str = ""
    adobe_cloud_session_store: str = "memory"
    aem_cloud_author_url: str = ""
    aem_cloud_provider_mode: str = "openapi"
    aem_cloud_direct_auth_mode: str = "local_token"
    aem_cloud_local_token: str = ""
    aem_cloud_test_path: str = ""
    # Page Management is experimental. Keep its verified API root/version
    # replaceable without exposing either through MCP tool contracts.
    aem_cloud_pages_api_path: str = "/adobe/sites/pages"
    aem_cloud_assets_api_path: str = "/adobe/assets"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_adobe_cloud(self) -> "Settings":
        runtime = self.aem_runtime_mode.strip().lower()
        provider = self.aem_cloud_provider_mode.strip().lower()
        direct_auth = self.aem_cloud_direct_auth_mode.strip().lower()
        if runtime not in {"local", "cloud"}:
            raise ValueError("AEM_RUNTIME_MODE must be local or cloud.")
        if provider not in {"openapi", "direct_http"}:
            raise ValueError("AEM_CLOUD_PROVIDER_MODE must be openapi or direct_http.")
        if runtime == "cloud" and provider == "openapi" and not self.adobe_cloud_enabled:
            raise ValueError("ADOBE_CLOUD_ENABLED=true is required for cloud OpenAPI mode.")
        if self.adobe_cloud_enabled and not (runtime == "cloud" and provider == "direct_http"):
            if not self.adobe_cloud_client_id.strip():
                raise ValueError("ADOBE_CLOUD_CLIENT_ID is required when Adobe Cloud is enabled.")
            if not self.adobe_cloud_client_secret.strip():
                raise ValueError("ADOBE_CLOUD_CLIENT_SECRET is required when Adobe Cloud is enabled.")
            redirect = urlsplit(self.adobe_cloud_redirect_uri)
            local_http = redirect.scheme == "http" and redirect.hostname in {"localhost", "127.0.0.1", "::1"}
            if redirect.scheme != "https" and not local_http:
                raise ValueError("ADOBE_CLOUD_REDIRECT_URI must use HTTPS outside local development.")
        for name, value in (
            ("ADOBE_CLOUD_AUTHORIZATION_ENDPOINT", self.adobe_cloud_authorization_endpoint),
            ("ADOBE_CLOUD_TOKEN_ENDPOINT", self.adobe_cloud_token_endpoint),
            ("ADOBE_CLOUD_USERINFO_ENDPOINT", self.adobe_cloud_userinfo_endpoint),
        ):
            if urlsplit(value).scheme != "https":
                raise ValueError(f"{name} must use HTTPS.")
        if self.adobe_cloud_session_store.strip().lower() != "memory":
            raise ValueError("Only ADOBE_CLOUD_SESSION_STORE=memory is currently supported.")
        if self.aem_cloud_author_url.strip():
            author = urlsplit(self.aem_cloud_author_url)
            if author.scheme != "https":
                raise ValueError("AEM_CLOUD_AUTHOR_URL must use HTTPS when configured.")
            if not author.hostname or author.path not in {"", "/"} or author.query or author.fragment or author.username or author.password:
                raise ValueError("AEM_CLOUD_AUTHOR_URL must be an HTTPS origin without path, query, fragment, or userinfo.")
        elif runtime == "cloud":
            raise ValueError("AEM_CLOUD_AUTHOR_URL is required when AEM_RUNTIME_MODE=cloud.")
        if runtime == "cloud" and provider == "direct_http":
            if direct_auth != "local_token":
                raise ValueError("AEM_CLOUD_DIRECT_AUTH_MODE must be local_token.")
            if not self.aem_cloud_local_token.strip():
                raise ValueError("AEM_CLOUD_LOCAL_TOKEN is required for cloud direct HTTP mode.")
        for name, path in (("AEM_CLOUD_PAGES_API_PATH", self.aem_cloud_pages_api_path), ("AEM_CLOUD_ASSETS_API_PATH", self.aem_cloud_assets_api_path)):
            if not path.startswith("/") or "//" in path or ".." in path.split("/") or "?" in path or "#" in path:
                raise ValueError(f"{name} must be a safe origin-relative path.")
        return self

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
    def package_allowed_roots(self) -> tuple[str, ...]:
        return self._roots(self.aem_package_allowed_roots)

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
    def auth_mode(self) -> str:
        mode = self.mcp_auth_mode.strip().lower()
        if mode:
            return mode
        if self.mcp_oauth_enabled:
            return "oauth"
        return "static_bearer" if self.mcp_http_auth_enabled else "none"

    @property
    def oauth_required_scopes(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.mcp_oauth_required_scopes.split(",") if value.strip())

    @property
    def adobe_mcp_allowed_tool_names(self) -> frozenset[str]:
        return frozenset(
            value.strip() for value in self.adobe_mcp_allowed_tools.split(",") if value.strip()
        )

    @property
    def adobe_cloud_scope_names(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.adobe_cloud_scopes.replace(",", " ").split() if value.strip())

@lru_cache
def get_settings() -> Settings:
    return Settings()
