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

Phase 5.0.3: this is the one shared choke point every raw MCP call goes
through (kapruka_check_delivery, kapruka_create_order, kapruka_track_order,
kapruka_list_delivery_cities), so the span lives here rather than at each
higher-level call site (src/checkout/delivery.py, src/checkout/order.py) —
"no new scaffolding, just more call sites" from a single one, not several.
Business attributes (city, delivery_date, product_id, order total, ...)
come along for free by flattening the request `params`/parsed response
dict's own top-level scalar fields onto the span, rather than hand-picking
field names per tool — OTel span attributes only accept flat primitives,
not arbitrary nested structures, so anything not a plain
str/int/float/bool is skipped, with one deliberate exception: create_order's
own response nests the order total one level down under `summary`, which
is exactly the "order-total attribute" this phase asks for, so that one
level is flattened too. No manual error-status handling needed here —
`start_as_current_span`'s own default exception behavior (record + mark
ERROR) already covers a raised `KapurkaToolError`/network exception, same
as `run_turn`'s span already relies on.
"""

import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src import observability
from src.config import KAPRUKA_MCP_URL


class KapurkaToolError(RuntimeError):
    """Raised when a Kapruka MCP tool call returns isError=True."""


def _flatten_scalar_attrs(prefix: str, data: dict) -> dict:
    """Top-level scalar fields of a request/response dict, as span
    attributes — plus one level into a nested "summary" dict specifically
    (create_order's own response shape), since OTel attributes can't hold
    arbitrary nested structures.
    """
    attrs: dict = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)):
            attrs[f"{prefix}.{key}"] = value
        elif key == "summary" and isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, (str, int, float, bool)):
                    attrs[f"{prefix}.summary.{sub_key}"] = sub_value
    return attrs


async def call_kapruka_tool(tool_name: str, params: dict) -> dict:
    """Call one Kapruka MCP tool with response_format=json, return the parsed dict."""
    tracer = observability.get_tracer()
    with tracer.start_as_current_span(f"mcp.{tool_name}") as span:
        for key, value in _flatten_scalar_attrs("mcp.request", params).items():
            span.set_attribute(key, value)

        async with streamable_http_client(KAPRUKA_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool_name, {"params": {**params, "response_format": "json"}}
                )
                if result.isError:
                    text = result.content[0].text if result.content else "unknown error"
                    raise KapurkaToolError(f"{tool_name} failed: {text}")

                parsed = json.loads(result.structuredContent["result"])
                for key, value in _flatten_scalar_attrs("mcp.response", parsed).items():
                    span.set_attribute(key, value)
                return parsed
