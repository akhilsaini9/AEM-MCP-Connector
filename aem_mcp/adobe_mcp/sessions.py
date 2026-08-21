from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

from ..config import Settings, get_settings
from .auth import InMemoryAdobeTokenStore, oauth_callback_broker
from .client import AdobeMCPClient
from .errors import AdobeMCPAuthRequiredError, AdobeMCPDisabledError


class AdobeMCPSessionManager:
    """Concurrency-safe per-principal downstream session registry."""

    DEVELOPMENT_SESSION_KEY = "single-developer-poc"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.adobe_mcp_session_store != "memory":
            raise ValueError("Only ADOBE_MCP_SESSION_STORE=memory is supported in this POC.")
        self.token_store = InMemoryAdobeTokenStore()
        self._clients: dict[str, AdobeMCPClient] = {}
        self._lock = asyncio.Lock()

    def session_key(self) -> str:
        access_token = get_access_token()
        if access_token is not None and access_token.subject:
            return f"mcp-subject:{access_token.subject}"
        if self.settings.adobe_mcp_single_developer_mode:
            return self.DEVELOPMENT_SESSION_KEY
        raise AdobeMCPAuthRequiredError(
            "Trusted MCP user identity is unavailable. Enable the explicitly non-multi-user-safe single developer mode only for development."
        )

    def _ensure_enabled(self) -> None:
        if not self.settings.adobe_mcp_enabled:
            raise AdobeMCPDisabledError("Downstream Adobe MCP support is disabled.")

    async def get_client(self) -> AdobeMCPClient:
        self._ensure_enabled()
        key = self.session_key()
        async with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = AdobeMCPClient(self.settings, key, self.token_store)
                self._clients[key] = client
            return client

    async def status(self) -> dict[str, Any]:
        if not self.settings.adobe_mcp_enabled:
            return {"enabled": False, "connected": False, "authentication_required": False, "session_state": "disabled", "server": "Adobe AEM MCP", "tool_count": None}
        client = await self.get_client()
        return client.status()

    async def disconnect(self) -> dict[str, Any]:
        self._ensure_enabled()
        key = self.session_key()
        async with self._lock:
            client = self._clients.pop(key, None)
        if client:
            await client.close()
        await self.token_store.delete(key)
        return {"success": True, "connected": False, "session_state": "disconnected", "local_tokens_cleared": True}

    async def complete_callback(self, *, code: str, state: str, iss: str | None) -> str:
        return await oauth_callback_broker.complete(code=code, state=state, iss=iss)


adobe_mcp_sessions = AdobeMCPSessionManager()
