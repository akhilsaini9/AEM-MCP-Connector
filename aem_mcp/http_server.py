from __future__ import annotations

import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import HTMLResponse

from .config import Settings, get_settings
from .server import mcp
from .adobe_mcp.errors import AdobeMCPError
from .adobe_mcp.sessions import adobe_mcp_sessions

logger = logging.getLogger("aem_mcp.http")


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "transport": "streamable-http"})


@mcp.custom_route("/adobe-mcp/oauth/callback", methods=["GET"])
async def adobe_mcp_oauth_callback(request: Request) -> HTMLResponse:
    """One-time OAuth callback. Query values are never logged or returned."""
    try:
        await adobe_mcp_sessions.complete_callback(
            code=request.query_params.get("code", ""),
            state=request.query_params.get("state", ""),
            iss=request.query_params.get("iss"),
        )
        return HTMLResponse("Adobe authorization completed. You may close this window.")
    except AdobeMCPError:
        return HTMLResponse("Adobe authorization callback was invalid or expired.", status_code=400)


class BearerAuthAndLoggingMiddleware:
    """Authenticate MCP requests and emit metadata-only structured logs."""

    def __init__(self, app: Any, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.mcp_path = settings.mcp_path.rstrip("/") or "/mcp"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        path = request.url.path
        status_code = 500

        async def send_with_status(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        is_mcp_request = path == self.mcp_path or path.startswith(self.mcp_path + "/")
        if is_mcp_request and self.settings.mcp_http_auth_enabled:
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {self.settings.mcp_http_bearer_token}"
            if not hmac.compare_digest(supplied, expected):
                response = JSONResponse(
                    {"error": "unauthorized", "message": "A valid bearer token is required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send_with_status)
                self._log(request, request_id, status_code, started, authenticated=False)
                return

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            self._log(request, request_id, status_code, started, authenticated=True)

    @staticmethod
    def _log(
        request: Request,
        request_id: str,
        status_code: int,
        started: float,
        *,
        authenticated: bool,
    ) -> None:
        # Deliberately excludes query strings, bodies, Authorization, and all secrets.
        logger.info(
            json.dumps(
                {
                    "event": "mcp_http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "client": request.client.host if request.client else None,
                    "status": status_code,
                    "authenticated": authenticated,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
                separators=(",", ":"),
            )
        )


def resolve_http_host(settings: Settings) -> str:
    """Cloud Run sets PORT; use that as the signal to bind all interfaces."""
    return "0.0.0.0" if os.getenv("PORT", "").strip() else settings.mcp_host


def resolve_http_port(settings: Settings) -> int:
    """Resolve Cloud Run PORT first, then configured MCP_PORT, then its 8000 default."""
    cloud_run_port = os.getenv("PORT", "").strip()
    if cloud_run_port:
        try:
            return int(cloud_run_port)
        except ValueError as exc:
            raise ValueError("PORT must be an integer.") from exc
    return settings.mcp_port


def _validate_http_settings(settings: Settings) -> None:
    if not settings.mcp_path.startswith("/"):
        raise ValueError("MCP_PATH must start with '/'.")
    port = resolve_http_port(settings)
    if not 1 <= port <= 65535:
        raise ValueError("PORT/MCP_PORT must be between 1 and 65535.")
    if settings.mcp_http_auth_enabled and (
        not settings.mcp_http_bearer_token
        or settings.mcp_http_bearer_token == "change-me"
    ):
        raise ValueError(
            "MCP_HTTP_AUTH_ENABLED=true requires a non-placeholder MCP_HTTP_BEARER_TOKEN."
        )
    if settings.mcp_http_max_body_bytes <= 0:
        raise ValueError("MCP_HTTP_MAX_BODY_BYTES must be positive.")


def create_http_app(settings: Settings | None = None) -> Any:
    settings = settings or get_settings()
    _validate_http_settings(settings)

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.http_allowed_hosts,
        allowed_origins=settings.http_allowed_origins,
    )
    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        max_request_body_size=settings.mcp_http_max_body_bytes,
        transport_security=security,
        host=resolve_http_host(settings),
    )
    return BearerAuthAndLoggingMiddleware(app, settings=settings)


def run_http_server() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.mcp_http_log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = create_http_app(settings)
    uvicorn.run(
        app,
        host=resolve_http_host(settings),
        port=resolve_http_port(settings),
        log_level=settings.mcp_http_log_level.lower(),
        access_log=False,
    )
