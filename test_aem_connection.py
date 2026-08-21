import asyncio
import json
import sys
from aem_mcp.aem_client import AEMReadClient

async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/content"
    client = AEMReadClient()
    print(f"Testing AEM: {client.settings.aem_base_url}")
    print(f"Searching pages under: {path}")
    result = await client.search_pages(path, limit=5)
    print(json.dumps(result, indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(main())
