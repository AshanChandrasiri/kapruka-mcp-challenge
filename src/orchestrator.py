"""ConciergeOrchestrator — the root graph. Dispatches on the Intent
Router's result.

Not a purely sequential graph — every node in a fixed sequence would run
unconditionally, with no way to skip based on the classification result.
Not the supervisor/conditional-edge-by-agent-reasoning pattern either — that
pattern's routing decision comes from an agent node's own reasoning; this
dispatches on a plain string the router already returned. A custom
StateGraph is LangGraph's graph-based pattern for exactly this shape of
conditional routing.

The Intent Router reads this graph's own `messages` state directly — not a
second compiled-graph invocation with its own message list, which would
append the inbound message twice — but its OWN generated turn (the
classification call's AIMessage/tool-call plumbing) is deliberately NOT
merged back into shared `messages`. Only `structured_response` is returned.
**Found live, not assumed:** an earlier version embedded the router as a
literal subgraph node (`add_node("intent_router", build_router_agent())`),
which does share `messages` both ways — its own classification-turn
AIMessage got appended to shared history. That's harmless on its own, but
once the Gift-Picker (a second real model call in the same turn) reads that
history, Gemini rejects the request: "final request turn must be a user
message or a function response" — the API has no prefill support, and the
shared history now ended in an assistant turn with nothing after it. Fixed
by wrapping the router in `_run_intent_router` below, which calls it with
`state["messages"]` as input but only ever returns `structured_response` —
the customer-facing transcript should never have contained the router's
internal turn regardless of this bug.

History bounding: no bound at this top-level graph. The Gift-Picker needs
full history as working memory, and there's no way to know in advance —
before the router has classified the message — whether a given turn will
even reach it. The router's own bounded path lives only in
`classify_intent()`'s standalone call (src/router/intent_router.py), used
for isolated testing.

The Gift-Picker is embedded the same way as the router — a real subgraph
node sharing `messages`/`product_suggestions`/`cart` with this graph (see
src/gift_picker/state.py for why matching field names is what makes that
automatic). Detecting "did propose_cart fire THIS turn" (not a stale cart
from an earlier turn) is done by snapshotting `cart` immediately before the
Gift-Picker runs and comparing it against `cart` immediately after — a
change means propose_cart ran this turn. An EphemeralValue signal (as used
for the router's structured_response below) doesn't work for this: it only
survives exactly one step past the write, but the Gift-Picker's own ReAct
loop always needs one more internal step after a tool call (the model's
reply acknowledging the tool result) before the subgraph itself returns —
so an ephemeral flag set inside that subgraph would already be cleared by
the time control returns to this parent graph. A plain before/after value
comparison across the two real parent-level steps (reset -> Gift-Picker)
has no such timing gap.
"""

from typing import Annotated, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.gift_picker.agent import build_gift_picker_agent
from src.gift_picker.state import Cart
from src.prompts import CHITCHAT_RESPONSE, OUT_OF_SCOPE_RESPONSE
from src.router.intent_router import Intent, IntentClassification, build_router_agent
from src.session import get_checkpointer


class ConciergeState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    # Only needed transiently to hand the router's result to _extract_intent
    # within the same turn — EphemeralValue keeps it out of the checkpoint
    # entirely, avoiding a custom-pydantic-type msgpack persistence warning.
    structured_response: Annotated[Optional[IntentClassification], EphemeralValue]
    intent: Optional[Intent]
    product_suggestions: Optional[list[dict]]
    cart: Optional[Cart]
    # Orchestrator-only bookkeeping (the Gift-Picker never reads/writes this):
    # cart's value immediately before this turn's Gift-Picker run, so
    # _route_after_gift_picker can tell a fresh propose_cart call apart from
    # a cart left over from an earlier turn.
    cart_snapshot: Optional[Cart]


async def _run_intent_router(state: ConciergeState) -> dict:
    router = build_router_agent()
    result = await router.ainvoke({"messages": state["messages"]})
    return {"structured_response": result["structured_response"]}


def _extract_intent(state: ConciergeState) -> dict:
    classification = state["structured_response"]
    return {"intent": classification.intent}


def _route_on_intent(state: ConciergeState) -> str:
    return {
        "gift_request": "reset_before_gift_picker",
        "track_order": "track_order_stub",
        "return_item": "return_item_stub",
        "chitchat": "chitchat_node",
        "out_of_scope": "out_of_scope_node",
    }[state["intent"]]


def _chitchat_node(state: ConciergeState) -> dict:
    return {"messages": [AIMessage(content=CHITCHAT_RESPONSE)]}


def _out_of_scope_node(state: ConciergeState) -> dict:
    return {"messages": [AIMessage(content=OUT_OF_SCOPE_RESPONSE)]}


def _reset_before_gift_picker(state: ConciergeState) -> dict:
    """Structural reset, not left to the model to remember: product_suggestions
    is stale from any earlier turn by the time we get here, and cart_snapshot
    captures the pre-turn cart for _route_after_gift_picker's diff below.
    """
    return {"product_suggestions": [], "cart_snapshot": state.get("cart")}


def _route_after_gift_picker(state: ConciergeState) -> str:
    if state.get("cart") != state.get("cart_snapshot"):
        return "cart_proposed_stub"
    return "no_cart_relay"


def _cart_proposed_stub(state: ConciergeState) -> dict:
    print("[stub] cart proposed this turn — checkout pipeline lands in Phase 3")
    return {}


def _no_cart_relay(state: ConciergeState) -> dict:
    print("[console] product_suggestions:", state.get("product_suggestions"))
    return {}


def _track_order_stub(state: ConciergeState) -> dict:
    print("[stub] track_order intent — Phase 4 branch not built yet")
    return {
        "messages": [
            AIMessage(
                content="I can't check order status yet (that part's still being "
                "built), but I heard you."
            )
        ]
    }


def _return_item_stub(state: ConciergeState) -> dict:
    print("[stub] return_item intent — Phase 5 branch not built yet")
    return {
        "messages": [
            AIMessage(
                content="I can't process returns yet (that part's still being "
                "built), but I heard you."
            )
        ]
    }


async def build_orchestrator():
    """Compile the root ConciergeOrchestrator graph against the shared checkpointer."""
    graph = StateGraph(ConciergeState)

    graph.add_node("intent_router", _run_intent_router)
    graph.add_node("extract_intent", _extract_intent)
    graph.add_node("chitchat_node", _chitchat_node)
    graph.add_node("out_of_scope_node", _out_of_scope_node)
    graph.add_node("reset_before_gift_picker", _reset_before_gift_picker)
    graph.add_node("gift_picker", await build_gift_picker_agent())
    graph.add_node("cart_proposed_stub", _cart_proposed_stub)
    graph.add_node("no_cart_relay", _no_cart_relay)
    graph.add_node("track_order_stub", _track_order_stub)
    graph.add_node("return_item_stub", _return_item_stub)

    graph.add_edge(START, "intent_router")
    graph.add_edge("intent_router", "extract_intent")
    graph.add_conditional_edges("extract_intent", _route_on_intent)
    graph.add_edge("chitchat_node", END)
    graph.add_edge("out_of_scope_node", END)
    graph.add_edge("reset_before_gift_picker", "gift_picker")
    graph.add_conditional_edges("gift_picker", _route_after_gift_picker)
    graph.add_edge("cart_proposed_stub", END)
    graph.add_edge("no_cart_relay", END)
    graph.add_edge("track_order_stub", END)
    graph.add_edge("return_item_stub", END)

    checkpointer = await get_checkpointer()
    return graph.compile(checkpointer=checkpointer, name="concierge_orchestrator")
