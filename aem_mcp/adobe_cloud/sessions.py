from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol

from mcp.server.auth.middleware.auth_context import get_access_token

from .errors import AdobeCloudAuthenticationError, AdobeCloudOAuthStateError
from .errors import AdobeCloudError, AdobeCloudTokenExchangeError, AdobeCloudUserInfoError
from ..audit import audit_adobe_cloud
from ..config import Settings, get_settings


@dataclass
class AdobeCloudSession:
    session_key: str
    access_token: str = ""
    refresh_token: str | None = None
    expires_at: float = 0
    token_type: str = "Bearer"
    adobe_user_id: str = ""
    adobe_email: str | None = None
    connected_at: str | None = None
    author_url: str | None = None


@dataclass
class PendingOAuthState:
    session_key: str
    expires_at: float


class AdobeCloudSessionStore(Protocol):
    async def get(self, session_key: str) -> AdobeCloudSession | None: ...
    async def save(self, session: AdobeCloudSession) -> None: ...
    async def delete(self, session_key: str) -> None: ...
    async def save_state(self, state: str, pending: PendingOAuthState) -> None: ...
    async def consume_state(self, state: str, now: float) -> str: ...


class MemoryAdobeCloudSessionStore:
    """Process-local POC store with isolated sessions and atomic one-use state."""

    def __init__(self) -> None:
        self._sessions: dict[str, AdobeCloudSession] = {}
        self._states: dict[str, PendingOAuthState] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_key: str) -> AdobeCloudSession | None:
        async with self._lock:
            item = self._sessions.get(session_key)
            return replace(item) if item else None

    async def save(self, session: AdobeCloudSession) -> None:
        async with self._lock:
            self._sessions[session.session_key] = replace(session)

    async def delete(self, session_key: str) -> None:
        async with self._lock:
            self._sessions.pop(session_key, None)

    async def save_state(self, state: str, pending: PendingOAuthState) -> None:
        async with self._lock:
            self._states[state] = pending

    async def consume_state(self, state: str, now: float) -> str:
        if not state:
            raise AdobeCloudOAuthStateError("OAuth state is missing or invalid.")
        async with self._lock:
            pending = self._states.pop(state, None)
        if pending is None or pending.expires_at <= now:
            raise AdobeCloudOAuthStateError("OAuth state is invalid, expired, or already used.")
        return pending.session_key


def current_session_key() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise AdobeCloudAuthenticationError("Trusted MCP user identity is unavailable.")
    return f"mcp-subject:{token.subject}"


def connected_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdobeCloudSessionManager:
    def __init__(self, settings: Settings | None = None, *, store: AdobeCloudSessionStore | None = None, oauth: object | None = None, api_client: object | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = store or MemoryAdobeCloudSessionStore()
        if oauth is None:
            from .auth import AdobeCloudOAuth
            oauth = AdobeCloudOAuth(self.settings, self.store)
        if api_client is None:
            from .client import AdobeCloudClient
            api_client = AdobeCloudClient(self.settings)
        self.oauth = oauth
        self.api_client = api_client

    def session_key(self) -> str:
        return current_session_key()

    async def status(self, session_key: str | None = None) -> dict[str, object]:
        author_configured = bool(self.settings.aem_cloud_author_url.strip())
        if not self.settings.adobe_cloud_enabled:
            return {"enabled": False, "connected": False, "session_state": "disabled"}
        key = session_key or self.session_key()
        session = await self.store.get(key)
        connected = bool(session and session.access_token and session.expires_at > time.time())
        result: dict[str, object] = {
            "enabled": True, "connected": connected, "authentication_required": not connected,
            "session_state": "connected" if connected else "disconnected", "user_session_present": connected,
            "author_url_configured": author_configured,
        }
        if connected and session:
            result["adobe_user"] = {
                "subject_hash": hashlib.sha256(session.adobe_user_id.encode()).hexdigest()[:16],
            }
            result["last_connected_at"] = session.connected_at
        return result

    async def connect(self) -> dict[str, object]:
        if not self.settings.adobe_cloud_enabled:
            return {"enabled": False, "connected": False, "session_state": "disabled"}
        key = self.session_key()
        started = time.monotonic()
        try:
            try:
                await self.get_valid_access_token(key)
                return {"enabled": True, "connected": True, "authentication_required": False, "session_state": "connected"}
            except AdobeCloudAuthenticationError:
                url = await self.oauth.authorization_url(key)  # type: ignore[attr-defined]
                audit_adobe_cloud(self.settings, "adobe_cloud_connect_started", key, success=True, duration_ms=(time.monotonic()-started)*1000)
                return {"enabled": True, "connected": False, "authentication_required": True, "session_state": "authorization_required", "authorization_url": url}
        except AdobeCloudError as exc:
            audit_adobe_cloud(self.settings, "adobe_cloud_connect_started", key, success=False, duration_ms=(time.monotonic()-started)*1000, error_code=exc.code)
            raise

    async def complete_callback(self, *, code: str, state: str, oauth_error: str | None = None) -> None:
        started = time.monotonic()
        key: str | None = None
        try:
            key = await self.store.consume_state(state, time.time())
            if oauth_error:
                raise AdobeCloudAuthenticationError("Adobe authorization was not completed.", code="ADOBE_CLOUD_OAUTH_DENIED")
            token = await self.oauth.exchange_code(code)  # type: ignore[attr-defined]
            identity = await self.oauth.userinfo(token.access_token, token.token_type)  # type: ignore[attr-defined]
            await self.oauth.establish_session(key, token, identity, self.settings.aem_cloud_author_url)  # type: ignore[attr-defined]
            audit_adobe_cloud(self.settings, "adobe_cloud_oauth_callback_success", key, success=True, duration_ms=(time.monotonic()-started)*1000)
        except AdobeCloudError as exc:
            audit_adobe_cloud(self.settings, "adobe_cloud_oauth_callback_failure", key, success=False, duration_ms=(time.monotonic()-started)*1000, error_code=exc.code)
            raise

    async def get_valid_access_token(self, session_key: str | None = None) -> str:
        key = session_key or self.session_key()
        session = await self.store.get(key)
        if not session or not session.access_token:
            raise AdobeCloudAuthenticationError("Adobe Cloud connection is required.")
        if session.expires_at > time.time() + 30:
            return session.access_token
        if not session.refresh_token:
            await self.store.delete(key)
            raise AdobeCloudAuthenticationError("Adobe Cloud connection has expired; reconnect is required.")
        started = time.monotonic()
        try:
            token = await self.oauth.refresh(session.refresh_token)  # type: ignore[attr-defined]
            session.access_token = token.access_token
            session.expires_at = time.time() + token.expires_in
            session.token_type = token.token_type
            session.refresh_token = token.refresh_token or session.refresh_token
            await self.store.save(session)
            audit_adobe_cloud(self.settings, "adobe_cloud_token_refresh_success", key, success=True, duration_ms=(time.monotonic()-started)*1000)
            return session.access_token
        except AdobeCloudTokenExchangeError as exc:
            await self.store.delete(key)
            audit_adobe_cloud(self.settings, "adobe_cloud_token_refresh_failure", key, success=False, duration_ms=(time.monotonic()-started)*1000, error_code=exc.code)
            raise AdobeCloudAuthenticationError("Adobe Cloud connection has expired; reconnect is required.") from exc

    async def disconnect(self) -> dict[str, object]:
        key = self.session_key()
        started = time.monotonic()
        await self.store.delete(key)
        audit_adobe_cloud(self.settings, "adobe_cloud_disconnect", key, success=True, duration_ms=(time.monotonic()-started)*1000)
        return {"success": True, "enabled": self.settings.adobe_cloud_enabled, "connected": False, "session_state": "disconnected"}

    async def test_connection(self) -> dict[str, object]:
        key = self.session_key()
        started = time.monotonic()
        try:
            token = await self.get_valid_access_token(key)
            result = await self.api_client.probe(token)  # type: ignore[attr-defined]
            audit_adobe_cloud(self.settings, "adobe_cloud_api_probe", key, success=bool(result.get("authorized", True)), duration_ms=(time.monotonic()-started)*1000)
            return result
        except AdobeCloudError as exc:
            audit_adobe_cloud(self.settings, "adobe_cloud_api_probe", key, success=False, duration_ms=(time.monotonic()-started)*1000, error_code=exc.code)
            raise
