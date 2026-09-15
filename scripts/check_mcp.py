"""Phase 0 check: MultiServerMCPClient connects to the Kapruka MCP server
and loads its tools.

Run: .venv/Scripts/python.exe scripts/check_mcp.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_mcp_adapters.client import MultiServerMCPClient

from src.config import KAPRUKA_MCP_URL


async def main() -> None:
    client = MultiServerMCPClient(
        {
            "kapruka": {
                "transport": "streamable_http",
                "url": KAPRUKA_MCP_URL,
            }
        }
    )
    tools = await client.get_tools()
    print(f"Loaded {len(tools)} tools from {KAPRUKA_MCP_URL}:")
    for t in tools:
        print(f"  - {t.name}: {t.description.splitlines()[0][:80]}")


if __name__ == "__main__":
    asyncio.run(main())
