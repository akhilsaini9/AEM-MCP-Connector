from __future__ import annotations


class AdobeMCPError(RuntimeError):
    code = "ADOBE_MCP_CONNECTION_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.code

    def safe_result(self) -> dict[str, str | bool]:
        return {"success": False, "error": self.code, "message": str(self)}


class AdobeMCPDisabledError(AdobeMCPError):
    code = "ADOBE_MCP_DISABLED"


class AdobeMCPAuthRequiredError(AdobeMCPError):
    code = "ADOBE_MCP_AUTH_REQUIRED"


class AdobeMCPToolNotAllowedError(AdobeMCPError):
    code = "ADOBE_MCP_TOOL_NOT_ALLOWED"


class AdobeMCPToolNotAvailableError(AdobeMCPError):
    code = "ADOBE_MCP_TOOL_NOT_AVAILABLE"
