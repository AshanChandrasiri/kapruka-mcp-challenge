"""Raw MCP client for the deterministic checkout pipeline — deliberately NOT
`MultiServerMCPClient`.

Every Kapruka tool wraps its arguments in a single `params` object and
supports `response_format: "json"` (confirmed live, not guessed — see
PLAN.md Phase 3). With `response_format: "json"`, the JSON payload comes
back double-encoded: the raw MCP `CallToolResult.structuredContent` is
`{"result": "<json string>"}` — a JSON string nested inside the dict, not
the parsed object itself. Confirmed directly against the live server:

    structuredContent = {'result': '{\\n  "city": "Colombo 03", ...\\n}'}

`MultiServerMCPClient` is built for an LLM's tool-calling loop (schema
exposure to the model, `interrupt()` wiring); none of that applies to this
deterministic call site, so this module talks to the server directly.
"""

import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.config import KAPRUKA_MCP_URL


class KapurkaToolError(RuntimeError):
    """Raised when a Kapruka MCP tool call returns isError=True."""


async def call_kapruka_tool(tool_name: str, params: dict) -> dict:
    """Call one Kapruka MCP tool with response_format=json, return the parsed dict."""
    async with streamable_http_client(KAPRUKA_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                tool_name, {"params": {**params, "response_format": "json"}}
            )
            if result.isError:
                text = result.content[0].text if result.content else "unknown error"
                raise KapurkaToolError(f"{tool_name} failed: {text}")

            return json.loads(result.structuredContent["result"])
