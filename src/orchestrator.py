"""ConciergeOrchestrator — the root graph. Dispatches on the Intent
Router's result.

Not a purely sequential graph — every node in a fixed sequence would run
unconditionally, with no way to skip based on the classification result.
Not the supervisor/conditional-edge-by-agent-reasoning pattern either — that
pattern's routing decision comes from an agent node's own reasoning; this
dispatches on a plain string the router already returned. A custom
StateGraph is LangGraph's graph-based pattern for exactly this shape of
conditional routing.

The Intent Router runs as a real subgraph node sharing this graph's own
`messages` channel — not a second compiled-graph invocation with its own
message list, which would append the inbound message twice. Its structured
result is read out of shared state (`structured_response`) and copied into
a plain `intent` string field for the conditional edge to key off.

History bounding: no bound at this top-level graph. Phase 2's Gift-Picker
needs full history as working memory, and there's no way to know in
advance — before the router has classified the message — whether a given
turn will even reach the Gift-Picker. The router's own bounded path lives
only in `classify_intent()`'s standalone call (src/router/intent_router.py),
used for isolated testing.
"""

from typing import Annotated, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

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


def _extract_intent(state: ConciergeState) -> dict:
    classification = state["structured_response"]
    return {"intent": classification.intent}


def _route_on_intent(state: ConciergeState) -> str:
    return {
        "gift_request": "gift_request_stub",
        "track_order": "track_order_stub",
        "return_item": "return_item_stub",
        "chitchat": "chitchat_node",
        "out_of_scope": "out_of_scope_node",
    }[state["intent"]]


def _chitchat_node(state: ConciergeState) -> dict:
    return {"messages": [AIMessage(content=CHITCHAT_RESPONSE)]}


def _out_of_scope_node(state: ConciergeState) -> dict:
    return {"messages": [AIMessage(content=OUT_OF_SCOPE_RESPONSE)]}


def _gift_request_stub(state: ConciergeState) -> dict:
    print("[stub] gift_request intent — Gift-Picker agent lands in Phase 2")
    return {
        "messages": [
            AIMessage(
                content="Got it — you're after a gift! I can't actually search "
                "products yet (that part's still being built), but I heard you."
            )
        ]
    }


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

    graph.add_node("intent_router", build_router_agent())
    graph.add_node("extract_intent", _extract_intent)
    graph.add_node("chitchat_node", _chitchat_node)
    graph.add_node("out_of_scope_node", _out_of_scope_node)
    graph.add_node("gift_request_stub", _gift_request_stub)
    graph.add_node("track_order_stub", _track_order_stub)
    graph.add_node("return_item_stub", _return_item_stub)

    graph.add_edge(START, "intent_router")
    graph.add_edge("intent_router", "extract_intent")
    graph.add_conditional_edges("extract_intent", _route_on_intent)
    graph.add_edge("chitchat_node", END)
    graph.add_edge("out_of_scope_node", END)
    graph.add_edge("gift_request_stub", END)
    graph.add_edge("track_order_stub", END)
    graph.add_edge("return_item_stub", END)

    checkpointer = await get_checkpointer()
    return graph.compile(checkpointer=checkpointer, name="concierge_orchestrator")
