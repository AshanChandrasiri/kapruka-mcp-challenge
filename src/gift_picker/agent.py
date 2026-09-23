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

from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt
from langchain.agents.middleware.types import ModelRequest
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient

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
        _mcp_tools_cache = [t for t in all_tools if t.name in ALLOWED_MCP_TOOLS]
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
