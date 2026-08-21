from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

import httpx2
from mcp.client import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata
from pydantic import AnyUrl

from ..config import Settings
from .auth import AdobeTokenStore, SessionTokenStorage, oauth_callback_broker
from .errors import AdobeMCPAuthRequiredError, AdobeMCPError, AdobeMCPToolNotAllowedError


_SECRET_KEYS = {"access_token", "refresh_token", "authorization", "client_secret", "code", "code_verifier", "cookie", "set-cookie"}


def sanitize_downstream(value: Any, *, depth: int = 0) -> Any:
    """Bound downstream data and remove credential-shaped fields defensively."""
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:20000] + ("...[truncated]" if len(value) > 20000 else "")
    if isinstance(value, dict):
        return {
            str(key): sanitize_downstream(item, depth=depth + 1)
            for key, item in list(value.items())[:200]
            if str(key).lower() not in _SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_downstream(item, depth=depth + 1) for item in value[:200]]
    if hasattr(value, "model_dump"):
        return sanitize_downstream(value.model_dump(mode="json"), depth=depth + 1)
    return str(value)[:2000]


class AdobeMCPClient:
    """One user's downstream Adobe MCP connection and OAuth lifecycle."""

    def __init__(self, settings: Settings, session_key: str, token_store: AdobeTokenStore) -> None:
        self.settings = settings
        self.session_key = session_key
        self.token_store = token_store
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._authorization_ready = asyncio.Event()
        self._close_requested = asyncio.Event()
        self._callback_future: asyncio.Future[Any] | None = None
        self.authorization_url: str | None = None
        self.state = "disconnected"
        self.last_connected_at: str | None = None
        self.tool_count: int | None = None
        self.safe_error: dict[str, Any] | None = None

    async def _redirect(self, url: str) -> None:
        self.authorization_url = url
        self._callback_future = await oauth_callback_broker.register(self.session_key, url)
        self.state = "authentication_required"
        self._authorization_ready.set()

    async def _callback(self) -> Any:
        if self._callback_future is None:
            raise AdobeMCPError("Adobe OAuth callback was requested before authorization started.")
        return await self._callback_future

    async def _run_connect(self) -> None:
        stack = AsyncExitStack()
        try:
            metadata = OAuthClientMetadata(
                client_name="Custom AEM CRUD MCP",
                redirect_uris=[AnyUrl(self.settings.adobe_mcp_oauth_redirect_uri)],
            )
            oauth = OAuthClientProvider(
                server_url=self.settings.adobe_mcp_server_url,
                client_metadata=metadata,
                storage=SessionTokenStorage(self.token_store, self.session_key),
                redirect_handler=self._redirect,
                callback_handler=self._callback,
            )
            http_client = await stack.enter_async_context(httpx2.AsyncClient(
                auth=oauth,
                follow_redirects=True,
                timeout=self.settings.adobe_mcp_connect_timeout_seconds,
            ))
            streams = await stack.enter_async_context(streamable_http_client(
                self.settings.adobe_mcp_server_url, http_client=http_client
            ))
            session = await stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            self._stack = stack
            self._session = session
            self.state = "connected"
            self.last_connected_at = datetime.now(timezone.utc).isoformat()
            self.authorization_url = None
            self.safe_error = None
            self._authorization_ready.set()
            await self._close_requested.wait()
        except asyncio.CancelledError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            message = str(exc).lower()
            if "not permitted" in message or "not allow" in message or "registration" in message:
                code = "ADOBE_MCP_CLIENT_NOT_ALLOWLISTED"
            elif "403" in message or "forbidden" in message:
                code = "ADOBE_MCP_FORBIDDEN"
            elif "timeout" in message:
                code = "ADOBE_MCP_TIMEOUT"
            else:
                code = "ADOBE_MCP_CONNECTION_FAILED"
            self.safe_error = {"error": code, "message": "Adobe MCP connection could not be established."}
            self.state = "error"
            self._authorization_ready.set()

    async def connect(self) -> dict[str, Any]:
        if self._session is not None:
            return self.status()
        if not self.settings.adobe_mcp_oauth_redirect_uri:
            raise AdobeMCPAuthRequiredError("ADOBE_MCP_OAUTH_REDIRECT_URI must be configured.")
        if self._connect_task is None or self._connect_task.done():
            self.state = "connecting"
            self._authorization_ready.clear()
            self._close_requested.clear()
            self._connect_task = asyncio.create_task(self._run_connect())
        ready_task = asyncio.create_task(self._authorization_ready.wait())
        done, pending = await asyncio.wait(
            {self._connect_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            if task is ready_task:
                task.cancel()
        return self.status(include_authorization_url=True)

    def status(self, *, include_authorization_url: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": self.settings.adobe_mcp_enabled,
            "connected": self._session is not None,
            "authentication_required": self.state == "authentication_required",
            "session_state": self.state,
            "server": "Adobe AEM MCP",
            "tool_count": self.tool_count,
            "user_session_present": True,
            "last_connected_at": self.last_connected_at,
        }
        if include_authorization_url and self.authorization_url:
            result["authorization_url"] = self.authorization_url
            result["authorization_started"] = True
        if self.safe_error:
            result.update(self.safe_error)
        return result

    async def list_tools(self) -> list[dict[str, str | None]]:
        if self._session is None:
            raise AdobeMCPAuthRequiredError("Connect the Adobe MCP session first.")
        try:
            result = await self._session.list_tools()
        except (asyncio.TimeoutError, httpx2.TimeoutException) as exc:
            raise AdobeMCPError("Adobe MCP tools/list timed out.", code="ADOBE_MCP_TIMEOUT") from exc
        except Exception as exc:
            code = "ADOBE_MCP_FORBIDDEN" if "403" in str(exc) or "forbidden" in str(exc).lower() else "ADOBE_MCP_INVALID_RESPONSE"
            raise AdobeMCPError("Adobe MCP tools/list failed safely.", code=code) from exc
        self.tool_count = len(result.tools)
        return [{"name": tool.name, "description": tool.description} for tool in result.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in self.settings.adobe_mcp_allowed_tool_names:
            raise AdobeMCPToolNotAllowedError("The downstream Adobe MCP tool is not allowlisted.")
        if self._session is None:
            raise AdobeMCPAuthRequiredError("Connect the Adobe MCP session first.")
        try:
            result = await self._session.call_tool(name, arguments or {})
        except (asyncio.TimeoutError, httpx2.TimeoutException) as exc:
            raise AdobeMCPError("Adobe MCP tool call timed out.", code="ADOBE_MCP_TIMEOUT") from exc
        except Exception as exc:
            code = "ADOBE_MCP_FORBIDDEN" if "403" in str(exc) or "forbidden" in str(exc).lower() else "ADOBE_MCP_INVALID_RESPONSE"
            raise AdobeMCPError("Adobe MCP tool call failed safely.", code=code) from exc
        return sanitize_downstream(result)

    async def close(self) -> None:
        await oauth_callback_broker.cancel_session(self.session_key)
        if self._connect_task and not self._connect_task.done():
            self._close_requested.set()
            await asyncio.gather(self._connect_task, return_exceptions=True)
        if self._stack:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        self.state = "disconnected"
