"""Gift-Picker Agent — the only real ReAct-style reasoning loop in this
system: searches, evaluates results, refines, decides when the cart is good
enough. Tools scoped to search_products/get_product/list_categories only —
it never sees check_delivery/create_order/track_order.

Phase 3.6: still embedded as a literal, always-on subgraph node (unchanged
from Phase 1/2 — this mechanism was never the problem the Checkout Router
retirement fixed), but now also reachable mid-checkout via `handoff_reason`
(set by the shared `request_cart_revision` tool, from the Checkout Info or
Confirm agent). Since this node is built ONCE at graph-compile time, a
static `system_prompt` string can't reflect a value that changes turn to
turn — `dynamic_prompt` middleware solves that without rebuilding the node:
it re-runs at actual model-call time, inside this agent's own ReAct loop,
reading whatever `handoff_reason` is in the CURRENT turn's shared state.
"""

import asyncio
import re

from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt
from langchain.agents.middleware.types import ModelRequest
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient

from src import observability
from src.checkout.shared_tools import cancel_checkout
from src.config import GOOGLE_API_KEY, KAPRUKA_MCP_URL, LLM_MODEL
from src.gift_picker.state import GiftPickerState
from src.gift_picker.tools import confirm_cart_and_proceed, get_recipient_profile, propose_cart, suggest_products
from src.prompts import GIFT_PICKER_INSTRUCTIONS

ALLOWED_MCP_TOOLS = {
    "kapruka_search_products",
    "kapruka_get_product",
    "kapruka_list_categories",
}

_mcp_tools_cache: list | None = None
_mcp_tools_lock = asyncio.Lock()

_RESULT_COUNT_RE = re.compile(r"Showing (\d+) results?")
_STOCK_LINE_RE = re.compile(r"(In stock(?:\s*\([^)]*\))?|Out of stock)", re.IGNORECASE)
_PRODUCT_STOCK_RE = re.compile(r"\*\*Stock\*\*:\s*(.+)")


def _mcp_result_text(result) -> str:
    """kapruka_search_products/kapruka_get_product, loaded here via
    MultiServerMCPClient, return plain markdown text (a list of
    {"type": "text", "text": ...} blocks) — unlike
    src/checkout/mcp_client.py's own raw client, langchain_mcp_adapters
    doesn't expose a response_format=json option to the model here, so
    there's no structured result_count/in_stock field to just read; they
    have to be pulled out of the rendered text instead.

    **Found live, not assumed — two different result shapes, not one:**
    calling a tool directly via `.ainvoke()` (e.g., in a standalone check)
    returns a plain list of `{"type": "text", ...}` blocks, but invoked
    for real through the agent's own graph, `tool.coroutine` returns a
    `(content, artifact)` TUPLE instead (LangChain's `content_and_artifact`
    response shape) — `str()`-ing the whole tuple (the original
    implementation's fallback branch) stringified BOTH halves together,
    silently doubling every count (`_STOCK_LINE_RE.findall` counted 20 "in
    stock" mentions for a 10-result search; `_RESULT_COUNT_RE.search`'s
    single-match lookup stayed correct at 10, which is what exposed the
    mismatch — a live agent-driven run showed this, an isolated direct
    `.ainvoke()` call didn't reproduce it at all). Unwrapping the tuple
    first, then reading just the content list's first text block, is what
    actually fixes it for both call shapes.
    """
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("text"):
                return item["text"]
        return ""
    return str(result)


def _wrap_mcp_tool_with_span(tool):
    """Phase 5.0.5: gives kapruka_search_products/kapruka_get_product each
    their own small span per call, carrying result_count/in_stock —
    **not** `trace.get_current_span().set_attribute(...)` on whatever's
    already open, the way this phase's own plan first described it (same
    pattern used for run_turn's own gift.intent attribute). Verified live
    first: the bridge's own auto-instrumented span for a tool call is
    created on langsmith's background tracing thread (Phase 5.0.1's own
    finding), so it's never "current" from inside the tool's own
    execution — calling set_attribute there actually lands on whatever
    hand-written span IS current (confirmed live: run_turn), which would
    also silently overwrite itself if the same tool fires more than once
    in one turn (a search refinement, for instance). A dedicated span per
    call, same `mcp.{tool_name}` pattern Phase 5.0.3 already established
    for the raw MCP client, is what actually attaches these attributes to
    something meaningfully scoped to that one call.
    """
    original_coroutine = tool.coroutine

    async def _traced_coroutine(*args, **kwargs):
        with observability.get_tracer().start_as_current_span(f"tool.{tool.name}") as span:
            result = await original_coroutine(*args, **kwargs)
            text = _mcp_result_text(result)

            if tool.name == "kapruka_search_products":
                count_match = _RESULT_COUNT_RE.search(text)
                if count_match:
                    span.set_attribute("kapruka.result_count", int(count_match.group(1)))
                stock_mentions = _STOCK_LINE_RE.findall(text)
                if stock_mentions:
                    span.set_attribute(
                        "kapruka.in_stock_count",
                        sum(1 for m in stock_mentions if m.lower().startswith("in stock")),
                    )
            elif tool.name == "kapruka_get_product":
                stock_match = _PRODUCT_STOCK_RE.search(text)
                if stock_match:
                    span.set_attribute(
                        "kapruka.in_stock", stock_match.group(1).strip().lower().startswith("in stock")
                    )

            return result

    tool.coroutine = _traced_coroutine
    return tool


async def _load_scoped_mcp_tools() -> list:
    """Cached: the scoped MCP tool list doesn't change turn to turn."""
    global _mcp_tools_cache
    if _mcp_tools_cache is not None:
        return _mcp_tools_cache
    async with _mcp_tools_lock:
        if _mcp_tools_cache is not None:
            return _mcp_tools_cache
        client = MultiServerMCPClient(
            {"kapruka": {"transport": "streamable_http", "url": KAPRUKA_MCP_URL}}
        )
        all_tools = await client.get_tools()
        scoped = [t for t in all_tools if t.name in ALLOWED_MCP_TOOLS]
        for t in scoped:
            if t.name in ("kapruka_search_products", "kapruka_get_product"):
                _wrap_mcp_tool_with_span(t)
        _mcp_tools_cache = scoped
        return _mcp_tools_cache


@dynamic_prompt
def _gift_picker_prompt(request: ModelRequest) -> str:
    handoff_reason = request.state.get("handoff_reason")
    if not handoff_reason:
        return GIFT_PICKER_INSTRUCTIONS
    return (
        f"{GIFT_PICKER_INSTRUCTIONS}\n\n"
        "You're being consulted mid-checkout, not starting a fresh request — "
        f"here's why: {handoff_reason}\n"
        "Address that directly (propose an alternative, ask what they'd "
        "like instead, etc.) rather than restarting the conversation from "
        "scratch."
    )


async def build_gift_picker_agent():
    """Fresh agent instance — same "can't be reused across parents" reasoning
    as build_router_agent (Phase 1): a fresh one is built each time this is
    embedded as a subgraph node.
    """
    mcp_tools = await _load_scoped_mcp_tools()
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, google_api_key=GOOGLE_API_KEY)
    return create_agent(
        llm,
        tools=[*mcp_tools, get_recipient_profile, suggest_products, propose_cart, confirm_cart_and_proceed, cancel_checkout],
        middleware=[_gift_picker_prompt],
        state_schema=GiftPickerState,
    )
