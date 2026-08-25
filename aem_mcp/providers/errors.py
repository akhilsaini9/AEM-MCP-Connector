from __future__ import annotations

from typing import Any


class AEMProviderError(Exception):
    code = "provider_error"
    def __init__(self, message: str = "AEM provider operation failed.", *, code: str | None = None) -> None:
        super().__init__(message); self.safe_message = message
        if code: self.code = code
    def safe_result(self) -> dict[str, Any]:
        return {"success": False, "error": self.code, "message": self.safe_message}


class AEMProviderAuthenticationRequired(AEMProviderError): code = "adobe_authentication_required"
class AEMProviderPermissionDenied(AEMProviderError): code = "permission_or_client_registration_denied"
class AEMProviderNotFound(AEMProviderError): code = "aem_resource_not_found"
class AEMProviderUnsupported(AEMProviderError): code = "unsupported_in_cloud_mode"
class AEMProviderRateLimited(AEMProviderError): code = "aem_cloud_rate_limited"
class AEMProviderRemoteError(AEMProviderError): code = "aem_cloud_remote_error"
class AEMProviderConfigurationError(AEMProviderError): code = "aem_cloud_configuration_error"
