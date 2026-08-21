from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx2
from dotenv import load_dotenv
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client


PROJECT_DIR = Path(r"F:\custom-aem-crud-mcp")
ENV_FILE = PROJECT_DIR / ".env"
MCP_URL = "https://grazing-luckiness-hurling.ngrok-free.dev/mcp"
PAGE_PATH = "/content/sigma"


class UnauthorizedError(RuntimeError):
    """Raised when the MCP endpoint rejects the configured bearer token."""


async def reject_unauthorized(response: httpx2.Response) -> None:
    # An event hook preserves the HTTP status that the MCP transport otherwise
    # converts into a generic transport error. It does not construct JSON-RPC.
    if response.status_code == 401:
        await response.aread()
        raise UnauthorizedError(
            "HTTP 401 Unauthorized: MCP_HTTP_BEARER_TOKEN is missing, incorrect, "
            "or differs from the token used by the running server."
        )


def printable_tool_result(result: Any) -> Any:
    if result.structured_content is not None:
        return result.structured_content

    text_parts = [item.text for item in result.content if hasattr(item, "text")]
    if len(text_parts) == 1:
        try:
            return json.loads(text_parts[0])
        except json.JSONDecodeError:
            return text_parts[0]
    return text_parts


async def run() -> None:
    if not ENV_FILE.is_file():
        raise FileNotFoundError(f"Environment file not found: {ENV_FILE}")

    load_dotenv(ENV_FILE, override=False)
    token = os.getenv("MCP_HTTP_BEARER_TOKEN", "").strip()
    if not token or token == "change-me":
        raise ValueError(
            f"Set MCP_HTTP_BEARER_TOKEN to a real bearer token in {ENV_FILE}."
        )

    print(f"MCP Python SDK: {importlib.metadata.version('mcp')}")
    print(f"Connecting to: {MCP_URL}")

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(30.0),
        event_hooks={"response": [reject_unauthorized]},
    ) as http_client:
        async with streamable_http_client(
            MCP_URL,
            http_client=http_client,
        ) as streams:
            async with ClientSession(*streams) as session:
                try:
                    initialized = await session.initialize()
                except Exception as exc:
                    raise RuntimeError(f"MCP initialization failed: {exc}") from exc

                print(
                    "Initialized:",
                    f"{initialized.server_info.name} "
                    f"(protocol {initialized.protocol_version})",
                )

                tools_result = await session.list_tools()
                tool_names = [tool.name for tool in tools_result.tools]
                print("\ntools/list:")
                for name in tool_names:
                    print(f"  - {name}")

                if "get_page_properties" not in tool_names:
                    raise RuntimeError("MCP server did not advertise get_page_properties.")

                try:
                    page_result = await session.call_tool(
                        "get_page_properties",
                        {"path": PAGE_PATH},
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"MCP tool call get_page_properties failed: {exc}"
                    ) from exc

                if page_result.is_error:
                    details = printable_tool_result(page_result)
                    raise RuntimeError(
                        "MCP tool call get_page_properties returned an error: "
                        f"{details}"
                    )

                print(f"\nget_page_properties({PAGE_PATH!r}):")
                print(json.dumps(printable_tool_result(page_result), indent=2, default=str))


def main() -> int:
    try:
        asyncio.run(run())
        return 0
    except UnauthorizedError as exc:
        print(f"Authentication error: {exc}", file=sys.stderr)
    except (ConnectionRefusedError, httpx2.ConnectError) as exc:
        print(
            f"Connection refused: cannot reach {MCP_URL}. "
            f"Confirm python run_http_server.py is running. ({exc})",
            file=sys.stderr,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
    except RuntimeError as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"Unexpected error ({type(exc).__name__}): {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
