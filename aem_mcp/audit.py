from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, AsyncIterator

from .config import Settings

logger = logging.getLogger("aem_mcp.audit")


def audit_package_event(settings: Settings, event: str, **metadata: Any) -> None:
    """Emit package lifecycle metadata; callers must never pass credentials/tokens."""
    if not settings.mcp_audit_log_enabled:
        return
    level = getattr(logging, settings.mcp_audit_log_level.upper(), logging.INFO)
    logger.log(level, json.dumps({
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **metadata,
    }, separators=(",", ":")))


def dry_run_result(operation: str, affected_paths: list[str], planned_changes: list[Any], warnings: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "operation": operation,
        "dry_run": True,
        "affected_paths": affected_paths,
        "planned_changes": planned_changes,
        "warnings": warnings or [],
        "requires_confirmation": True,
        **extra,
    }


def confirmation_error(operation: str, affected_paths: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "operation": operation,
        "dry_run": False,
        "affected_paths": affected_paths,
        "planned_changes": [],
        "warnings": warnings or [],
        "requires_confirmation": True,
        "success": False,
        "error": "Confirmation required: call with dry_run=false and confirm=true",
    }


@asynccontextmanager
async def audit_operation(settings: Settings, operation: str, path: str, dry_run: bool, confirm: bool) -> AsyncIterator[dict[str, Any]]:
    started = time.monotonic()
    state: dict[str, Any] = {"result": "unknown", "success": False}
    try:
        yield state
    except Exception as exc:
        state.update(result="failure", success=False, error=f"{type(exc).__name__}: {str(exc)[:500]}")
        raise
    finally:
        if settings.mcp_audit_log_enabled:
            level = getattr(logging, settings.mcp_audit_log_level.upper(), logging.INFO)
            logger.log(level, json.dumps({
                "event": "aem_write_audit",
                "operation": operation,
                "affected_path": path,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "result": state.get("result"),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "dry_run": dry_run,
                "confirm": confirm,
                "success": bool(state.get("success")),
                **({"error": state["error"]} if state.get("error") else {}),
            }, separators=(",", ":")))


@asynccontextmanager
async def audit_read(settings: Settings, operation: str, path: str) -> AsyncIterator[dict[str, Any]]:
    """Log bounded read metadata without ever accepting or recording response bytes."""
    started = time.monotonic()
    state: dict[str, Any] = {"success": False}
    try:
        yield state
    except Exception as exc:
        state.update(success=False, error=f"{type(exc).__name__}: {str(exc)[:500]}")
        raise
    finally:
        if settings.mcp_audit_log_enabled:
            level = getattr(logging, settings.mcp_audit_log_level.upper(), logging.INFO)
            logger.log(level, json.dumps({
                "event": "aem_read_audit",
                "operation": operation,
                "asset_path": path,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "success": bool(state.get("success")),
                **({"selected_rendition": state["selected_rendition"]} if state.get("selected_rendition") else {}),
                **({"mime_type": state["mime_type"]} if state.get("mime_type") else {}),
                **({"byte_count": state["byte_count"]} if state.get("byte_count") is not None else {}),
                **({"error": state["error"]} if state.get("error") else {}),
            }, separators=(",", ":")))


@asynccontextmanager
async def audit_adobe_mcp(
    settings: Settings,
    local_tool: str,
    upstream_tool: str | None,
    session_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """Metadata-only audit record for a downstream Adobe MCP operation."""
    started = time.monotonic()
    state: dict[str, Any] = {"success": False}
    try:
        yield state
    except Exception as exc:
        state.update(success=False, error=f"{type(exc).__name__}: {str(exc)[:300]}")
        raise
    finally:
        if settings.mcp_audit_log_enabled:
            level = getattr(logging, settings.mcp_audit_log_level.upper(), logging.INFO)
            logger.log(level, json.dumps({
                "event": "adobe_mcp_downstream_audit",
                "local_tool": local_tool,
                "upstream_tool": upstream_tool,
                "session_id": session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "success": bool(state.get("success")),
                **({"error": state["error"]} if state.get("error") else {}),
            }, separators=(",", ":")))
