from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import AuthorizationCodeResult

from .errors import AdobeMCPError


class AdobeTokenStore(Protocol):
    async def get(self, session_key: str) -> dict[str, Any] | None: ...
    async def save(self, session_key: str, token_data: dict[str, Any]) -> None: ...
    async def delete(self, session_key: str) -> None: ...


class InMemoryAdobeTokenStore:
    """Development-only token storage; replace with an encrypted durable store."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_key: str) -> dict[str, Any] | None:
        async with self._lock:
            record = self._records.get(session_key)
            return dict(record) if record is not None else None

    async def save(self, session_key: str, token_data: dict[str, Any]) -> None:
        async with self._lock:
            self._records[session_key] = dict(token_data)

    async def delete(self, session_key: str) -> None:
        async with self._lock:
            self._records.pop(session_key, None)


class SessionTokenStorage:
    """SDK TokenStorage adapter isolating tokens and client data by session key."""

    def __init__(self, store: AdobeTokenStore, session_key: str) -> None:
        self.store = store
        self.session_key = session_key

    async def _record(self) -> dict[str, Any]:
        return await self.store.get(self.session_key) or {}

    async def get_tokens(self) -> Any:
        return (await self._record()).get("tokens")

    async def set_tokens(self, tokens: Any) -> None:
        record = await self._record()
        record["tokens"] = tokens
        await self.store.save(self.session_key, record)

    async def get_client_info(self) -> Any:
        return (await self._record()).get("client_info")

    async def set_client_info(self, client_info: Any) -> None:
        record = await self._record()
        record["client_info"] = client_info
        await self.store.save(self.session_key, record)


@dataclass
class PendingAuthorization:
    session_key: str
    future: asyncio.Future[AuthorizationCodeResult]


class OAuthCallbackBroker:
    """One-use state router; the SDK remains responsible for validating state/iss."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingAuthorization] = {}
        self._lock = asyncio.Lock()

    async def register(self, session_key: str, authorization_url: str) -> asyncio.Future[AuthorizationCodeResult]:
        query = parse_qs(urlsplit(authorization_url).query)
        state = (query.get("state") or [""])[0]
        if not state:
            raise AdobeMCPError("Adobe authorization URL did not contain OAuth state.", code="ADOBE_MCP_INVALID_RESPONSE")
        future: asyncio.Future[AuthorizationCodeResult] = asyncio.get_running_loop().create_future()
        async with self._lock:
            if state in self._pending:
                raise AdobeMCPError("Duplicate Adobe OAuth state was rejected.", code="ADOBE_MCP_INVALID_RESPONSE")
            self._pending[state] = PendingAuthorization(session_key, future)
        return future

    async def complete(self, *, code: str, state: str, iss: str | None) -> str:
        if not code or not state:
            raise AdobeMCPError("Adobe OAuth callback is missing code or state.", code="ADOBE_MCP_INVALID_RESPONSE")
        async with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending.future.done():
            raise AdobeMCPError("Adobe OAuth state is invalid or was already used.", code="ADOBE_MCP_INVALID_RESPONSE")
        pending.future.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
        return pending.session_key

    async def cancel_session(self, session_key: str) -> None:
        async with self._lock:
            states = [state for state, item in self._pending.items() if item.session_key == session_key]
            pending = [self._pending.pop(state) for state in states]
        for item in pending:
            if not item.future.done():
                item.future.cancel()


oauth_callback_broker = OAuthCallbackBroker()
