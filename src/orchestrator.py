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

Phase 3 (checkout) adds a stage pre-check ahead of everything above:
`_route_from_start` reads `stage` before the Intent Router ever runs. A
`collecting_delivery`/`awaiting_confirm`/`resolving_delivery_conflict` stage
means a checkout is already in progress, and the customer's reply belongs
to that flow, not a fresh classification — see src/checkout/flow.py for the
state machine itself. `resolving_delivery_conflict` re-enters through the
same `reset_before_gift_picker -> gift_picker -> _route_after_gift_picker`
chain as a fresh `gift_request` — deliberately reused, since "the Gift-Picker
needs another turn" is the same move either way. The only difference is
where the cart-changed branch goes now: `start_checkout_node`
(src/checkout/flow.py) replaces what was a Phase 2 stub.

Phase 3.5 replaces the direct `stage` -> handler bypass with an LLM
classification step: `_route_from_start` now sends every checkout stage
(`collecting_delivery`/`awaiting_confirm`/`resolving_delivery_conflict`) to
`checkout_router` (a second, narrower classifier, src/router/checkout_router.py)
first — no stage still bypasses classification entirely, every turn goes
through one classifier or the other. Problem this fixes: a free-form
digression during `collecting_delivery` (e.g. "actually change the order")
used to be consumed as a literal field value, since that handler had no way
to tell "this is an answer" from "this is not an answer" — **confirmed
live**: a "what payment methods do you accept" tangent sent mid-
`collecting_delivery` used to land in `checkout_info[collecting_field]`
verbatim; a real cancel request sent during `resolving_delivery_conflict`
(before this fix routed that stage through the classifier too) got handed
straight to the Gift-Picker, which "cancelled" by emptying the cart via
`propose_cart` instead of a clean state clear — messier than the dedicated
`cancel_checkout` path below. `_run_checkout_router` follows the same
embedding pattern as `_run_intent_router` (fresh agent instance, full
`messages` history as input, only the classification fields returned) for
the same reason `_run_intent_router` does it: avoid polluting shared
`messages` with the classifier's own turn. **Widened after initial
implementation:** originally only `messages[-1]` was passed in (the
explicit `stage`/`collecting_field`/`cart_summary` snapshot was meant to be
sufficient on its own), but that throws away the same working-memory value
full history gives the Gift-Picker — e.g. a customer's phrasing earlier in
the conversation that disambiguates a short, otherwise-ambiguous reply.
Safe to widen for the same reason it was safe for the Intent Router: this
node never merges the classifier's own generated turn back into shared
`messages` (only `checkout_intent`/`extracted_value` are returned), so
`state["messages"]` going in still always ends on a real customer turn —
the exact invariant that mattered when this bug was first found on the
Intent Router (see above). Unlike the Intent Router's `structured_response`,
the Checkout Router's output fields (`checkout_intent`, `extracted_value`)
are plain strings — no EphemeralValue/msgpack workaround needed, since
there's no custom pydantic type being handed to the checkpointer.

`_route_on_checkout_intent` dispatches: `cancel_checkout` (any stage) to a
new terminal node that clears checkout state; at `resolving_delivery_conflict`,
everything else (there's no single field pending at that stage regardless
of classification — it's inherently a Gift-Picker mid-revision
conversation) goes straight back to the Gift-Picker exactly as it did
before this phase; otherwise `answers_pending` goes to whichever handler
matches the stage, and `modify_request`/`unrelated` go to
`handle_checkout_digression` (src/checkout/flow.py), which reuses the exact
same Gift-Picker hand-back mechanism Phase 3 built for a failed delivery
check and an unclear confirm reply. Hard rule, unchanged: `answers_pending`
at `awaiting_confirm` is a routing decision, not a confirmation —
`_is_confirmation` still independently gates `create_order` on the raw
message text every time, not on `extracted_value`.
"""

from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.checkout.flow import (
    cancel_checkout_node,
    handle_awaiting_confirm,
    handle_checkout_digression,
    handle_collecting_delivery,
    start_checkout_node,
)
from src.gift_picker.agent import build_gift_picker_agent
from src.gift_picker.state import Cart, ProductSuggestion
from src.prompts import CHITCHAT_RESPONSE, OUT_OF_SCOPE_RESPONSE
from src.router.checkout_router import CheckoutIntent, build_checkout_router_agent
from src.router.intent_router import Intent, IntentClassification, build_router_agent
from src.session import get_checkpointer

Stage = Literal["collecting_delivery", "resolving_delivery_conflict", "awaiting_confirm"]


class ConciergeState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    # Only needed transiently to hand the router's result to _extract_intent
    # within the same turn — EphemeralValue keeps it out of the checkpoint
    # entirely, avoiding a custom-pydantic-type msgpack persistence warning.
    structured_response: Annotated[Optional[IntentClassification], EphemeralValue]
    intent: Optional[Intent]
    product_suggestions: Optional[list[ProductSuggestion]]
    cart: Optional[Cart]
    # Orchestrator-only bookkeeping (the Gift-Picker never reads/writes this):
    # cart's value immediately before this turn's Gift-Picker run, so
    # _route_after_gift_picker can tell a fresh propose_cart call apart from
    # a cart left over from an earlier turn.
    cart_snapshot: Optional[Cart]
    # Checkout state machine (src/checkout/flow.py) — absent means no
    # checkout in progress.
    stage: Optional[Stage]
    checkout_info: Optional[dict]
    collecting_field: Optional[str]
    # Checkout Router's classification this turn (Phase 3.5) — only ever
    # set on a turn that actually ran checkout_router; plain strings, so
    # (unlike the Intent Router's structured_response) no EphemeralValue
    # workaround is needed to keep them checkpoint-safe.
    checkout_intent: Optional[CheckoutIntent]
    extracted_value: Optional[str]


def  _route_from_start(state: ConciergeState) -> str:
    stage = state.get("stage")
    if stage in ("collecting_delivery", "awaiting_confirm", "resolving_delivery_conflict"):
        return "checkout_router"
    return "intent_router"


def _summarize_checkout_for_router(state: ConciergeState) -> str:
    """Short, human-readable snapshot of the in-progress checkout for the
    Checkout Router's prompt — deliberately not the raw cart/checkout_info
    dicts (delivery_checked_cart etc. are orchestrator bookkeeping the
    classifier has no use for).
    """
    cart = state.get("cart") or {}
    items = ", ".join(
        item.get("name", item.get("product_id", "?")) for item in cart.get("items", [])
    ) or "(no items)"
    checkout_info = state.get("checkout_info") or {}
    collected = {
        key: value
        for key, value in checkout_info.items()
        if value and not key.startswith("_") and key not in (
            "delivery_checked", "delivery_checked_cart", "delivery_fee", "perishable_warnings",
        )
    }
    return f"items=[{items}], estimated_total={cart.get('estimated_total')}, collected_so_far={collected}"


async def _run_checkout_router(state: ConciergeState) -> dict:
    cart_summary = _summarize_checkout_for_router(state)
    router = build_checkout_router_agent(state["stage"], state.get("collecting_field"), cart_summary)
    # Full history, not just the latest message — same working-memory value
    # it gives the Gift-Picker. Safe here for the same reason it's safe for
    # _run_intent_router: this node never merges its own generated turn
    # back into shared `messages`, only the classification fields below.
    result = await router.ainvoke({"messages": state["messages"]})
    classification = result["structured_response"]
    return {"checkout_intent": classification.intent, "extracted_value": classification.extracted_value}


def _route_on_checkout_intent(state: ConciergeState) -> str:
    intent = state["checkout_intent"]
    stage = state["stage"]
    print(f'_route_on_checkout_intent --> {intent}')
    print(f'_route_on_checkout_intent, stage --> {stage}')

    if intent == "cancel_checkout":
        return "cancel_checkout_node"

    if stage == "resolving_delivery_conflict":
        # No single field is pending at this stage regardless of
        # classification (that's inherently a Gift-Picker mid-revision
        # conversation, not a structured field answer) — everything short
        # of an explicit cancel goes straight back to it, same as before
        # Phase 3.5 added this classifier.
        return "reset_before_gift_picker"

    if intent == "answers_pending":
        return "handle_awaiting_confirm" if stage == "awaiting_confirm" else "handle_collecting_delivery"
    return "handle_checkout_digression"


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
        return "start_checkout"
    return "no_cart_relay"


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
    graph.add_node("start_checkout", start_checkout_node)
    graph.add_node("checkout_router", _run_checkout_router)
    graph.add_node("handle_collecting_delivery", handle_collecting_delivery)
    graph.add_node("handle_awaiting_confirm", handle_awaiting_confirm)
    graph.add_node("handle_checkout_digression", handle_checkout_digression)
    graph.add_node("cancel_checkout_node", cancel_checkout_node)
    graph.add_node("no_cart_relay", _no_cart_relay)
    graph.add_node("track_order_stub", _track_order_stub)
    graph.add_node("return_item_stub", _return_item_stub)

    graph.add_conditional_edges(START, _route_from_start)
    graph.add_edge("intent_router", "extract_intent")
    graph.add_conditional_edges("extract_intent", _route_on_intent)
    graph.add_edge("chitchat_node", END)
    graph.add_edge("out_of_scope_node", END)
    graph.add_edge("reset_before_gift_picker", "gift_picker")
    graph.add_conditional_edges("gift_picker", _route_after_gift_picker)
    graph.add_edge("start_checkout", END)
    graph.add_conditional_edges("checkout_router", _route_on_checkout_intent)
    graph.add_edge("handle_collecting_delivery", END)
    graph.add_edge("handle_awaiting_confirm", END)
    graph.add_edge("handle_checkout_digression", END)
    graph.add_edge("cancel_checkout_node", END)
    graph.add_edge("no_cart_relay", END)
    graph.add_edge("track_order_stub", END)
    graph.add_edge("return_item_stub", END)

    checkpointer = await get_checkpointer()
    return graph.compile(checkpointer=checkpointer, name="concierge_orchestrator")
