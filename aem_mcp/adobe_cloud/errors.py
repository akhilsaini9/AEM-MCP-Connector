from __future__ import annotations

from typing import Any


class AdobeCloudError(Exception):
    code = "ADOBE_CLOUD_ERROR"

    def __init__(self, message: str = "Adobe Cloud operation failed.", *, code: str | None = None) -> None:
        super().__init__(message)
        self.safe_message = message
        if code:
            self.code = code

    def safe_result(self) -> dict[str, Any]:
        return {"success": False, "error_code": self.code, "message": self.safe_message}


class AdobeCloudConfigurationError(AdobeCloudError):
    code = "ADOBE_CLOUD_CONFIGURATION_ERROR"


class AdobeCloudAuthenticationError(AdobeCloudError):
    code = "ADOBE_CLOUD_AUTHENTICATION_REQUIRED"


class AdobeCloudOAuthStateError(AdobeCloudError):
    code = "ADOBE_CLOUD_INVALID_STATE"


class AdobeCloudTokenExchangeError(AdobeCloudError):
    code = "ADOBE_CLOUD_TOKEN_EXCHANGE_FAILED"


class AdobeCloudUserInfoError(AdobeCloudError):
    code = "ADOBE_CLOUD_USERINFO_FAILED"


class AdobeCloudPermissionError(AdobeCloudError):
    code = "ADOBE_CLOUD_INSUFFICIENT_PERMISSION"
