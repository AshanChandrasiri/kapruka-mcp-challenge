"""ConciergeOrchestrator — the root graph. Dispatches on the active
classifier/agent's result.

Not a purely sequential graph — every node in a fixed sequence would run
unconditionally, with no way to skip based on the classification result.
Not the supervisor/conditional-edge-by-agent-reasoning pattern either — that
pattern's routing decision comes from an agent node's own reasoning; the
Intent Router branch below dispatches on a plain string the router already
returned. A custom StateGraph is LangGraph's graph-based pattern for
exactly this shape of conditional routing.

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

The Gift-Picker, Checkout Info Agent, and Confirm Agent are all embedded
the same way — real subgraph nodes sharing `messages` plus whichever
custom fields their own state schema declares (see
src/gift_picker/state.py, src/checkout/checkout_info_agent.py,
src/checkout/confirm_agent.py for why matching field names is what makes
that automatic). Each is a literal, always-on node built once at compile
time; per-turn context (a mid-checkout handoff reason, the current cart,
today's date, the order summary) reaches their prompts via `dynamic_prompt`
middleware (re-evaluated at actual model-call time, inside each agent's own
ReAct loop) rather than by rebuilding the node fresh every turn, and reaches
their tools that need live cart/checkout_info via LangGraph's
`InjectedState` rather than asking the model to retype it as an argument.

Phase 3.6 replaced Phase 3's deterministic one-field-per-turn machinery and
Phase 3.5's Checkout Router classifier entirely — retired, not extended:
`src/router/checkout_router.py`, `_run_checkout_router`,
`_route_on_checkout_intent`, `handle_checkout_digression`,
`_invoke_gift_picker_for_revision`, `_hand_back_to_gift_picker`, the
`collecting_delivery`/`resolving_delivery_conflict`/`awaiting_confirm`
stage names, `_advance_checkout`, `handle_collecting_delivery`, `_ask`,
`FIELD_PROMPTS`, `_invalid_date_reason` are all gone. In their place: three
agents that own their own judgment (see src/gift_picker/agent.py's own
module docstring for why the Gift-Picker's embedding mechanism itself
didn't need to change), plus exactly one thing that's still deterministic —
see the hard-rule section below.

**Stages, final shape:** `with_gift_picker` (picking/revising a cart,
including any hand-back from the other two agents — one destination
either way, not two, as Phase 3.5's `resolving_delivery_conflict` used to
be a separate name for the same thing) | `checkout_info` (gathering
everything `kapruka_create_order` needs besides the cart) | `confirm`
(order summary shown, working toward a final yes/no) | absent (normal
flow, Intent Router runs).

**Single exit condition per stage — this is what actually fixes Phase
3.5's auto-advance complaint:** a `propose_cart` diff alone, even the very
first one, no longer auto-advances anywhere; only `confirm_cart_and_proceed`
firing (`cart_confirmed`) advances `with_gift_picker` -> `checkout_info`,
and only `finalize_checkout_info` firing (`checkout_info_finalized`)
advances `checkout_info` -> `confirm`. Both flags are reset structurally
right before the node that can set them runs (same pattern Phase 1/2 used
for `product_suggestions`), so re-entering a stage never trusts a stale
flag left over from earlier in the conversation.

**`handoff_reason` replaces `_invoke_gift_picker_for_revision`:** set by
the shared `request_cart_revision` tool (Checkout Info or Confirm agent,
`src/checkout/shared_tools.py`), read by the Gift-Picker's own
`dynamic_prompt` middleware, cleared structurally right after the
Gift-Picker node runs regardless of outcome. Routing itself stays here in
the parent graph, not inside the tool: a `Command(graph=Command.PARENT)`
call from inside a subgraph's tool was considered and rejected as untested
in this codebase; a conditional edge checking `handoff_reason` after each
checkout-agent node runs (the `_route_after_*` functions below) reuses the
exact plain-`Command(update=...)` mechanism `propose_cart` already proves
works, just watched from one level up.

**Found live, a second instance of the exact bug this module already
documents for the Intent Router:** routing straight from one embedded
agent's conditional edge into the NEXT embedded agent within the same turn
(`enter_checkout_info`, `enter_confirm`, `_handoff_to_gift_picker`) leaves
shared `messages` ending on the FIRST agent's own final reply — a normal
assistant turn, since its ReAct loop always produces one after a tool call
— and Gemini refuses a request that doesn't end on a user message or a
function response. This didn't show up until two real agents actually
chained within one turn, live. Fixed the same way Phase 3's
`_invoke_gift_picker_for_revision` already fixed an analogous case: each of
these three transition nodes injects a synthetic
`[System note — not from the customer: ...]` `HumanMessage` before handing
off, giving the next agent's model call a proper user turn to end on.
`_check_final_confirmation` -> `confirm_agent` needs no such note — it
makes no agent/LLM call itself, so `messages` still ends on the customer's
own real reply when `confirm_agent` picks it up.

**`cancel_checkout` is a shared tool now, not a router-reached node** — any
of the three agents can recognize "the customer wants out" and call it
directly; it clears `stage` to `None`, which every `_route_after_*`
function below treats as "done, nothing further to route this turn."

**The hard rule, unchanged in spirit since Phase 3 — this is the ONLY
deterministic gate left in the whole checkout pipeline:** as of Phase 3.7,
the check lives INSIDE the `confirm_agent` node itself (`_run_confirm_agent`
below), not in a separate node/routing branch the way Phase 3.6 had it.
`_route_from_start` routes `stage == "confirm"` straight to `confirm_agent`
regardless of `awaiting_final_yes` — that flag is only read inside the
wrapper now. `_run_confirm_agent` checks `awaiting_final_yes` first; if
set, it runs `is_confirmation` (src/checkout/flow.py) directly on the
customer's raw reply — no agent/LLM call at all on this branch. Pass ->
`complete_order` (the only call site `kapruka_create_order` has anywhere in
this codebase, reachable only from here) and returns its result directly.
Fail -> clears `awaiting_final_yes` and falls through, in the same
function call, to the actual compiled Confirm Agent — same turn, no extra
graph hop, to actually figure out what the customer meant. `awaiting_final_yes`
false/unset skips the check entirely (covers `enter_confirm`'s own first
entry). The Confirm Agent's own `ask_final_confirmation` tool only ARMS
this gate (sets `awaiting_final_yes`); it never itself decides the outcome,
and it never has `kapruka_create_order` as a tool — verifiable by
inspection, exactly the property Phase 3 built this hard rule around in
the first place. Folding the check into the node (Phase 3.7) collapses
what used to be two nodes and an extra `_route_from_start` branch
(`check_final_confirmation`, `_route_after_final_confirmation_check`) into
one — the safety property itself (plain code, runs before any LLM call,
single `complete_order` call site) is unchanged, only where it physically
lives moved.
"""

from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.checkout.checkout_info_agent import build_checkout_info_agent
from src.checkout.confirm_agent import build_confirm_agent
from src.checkout.flow import complete_order, is_confirmation
from src.gift_picker.agent import build_gift_picker_agent
from src.gift_picker.state import Cart, ProductSuggestion
from src.prompts import CHITCHAT_RESPONSE, OUT_OF_SCOPE_RESPONSE
from src.router.intent_router import Intent, IntentClassification, build_router_agent
from src.session import get_checkpointer

Stage = Literal["with_gift_picker", "checkout_info", "confirm"]


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
    # Checkout state machine — absent means no checkout in progress.
    stage: Optional[Stage]
    checkout_info: Optional[dict]
    # Set by confirm_cart_and_proceed; reset False before every Gift-Picker
    # run. Absent this reset, an earlier turn's confirmation would look
    # live forever.
    cart_confirmed: Optional[bool]
    # Set by finalize_checkout_info; reset False before every Checkout Info
    # Agent run, same reasoning as cart_confirmed — a checkout_info dict
    # that already has all six fields from BEFORE a revision would
    # otherwise look finalized again without the agent re-validating it.
    checkout_info_finalized: Optional[bool]
    # Non-null while a checkout agent is consulting the Gift-Picker
    # mid-checkout (request_cart_revision); read by the Gift-Picker's own
    # dynamic prompt, cleared right after it runs.
    handoff_reason: Optional[str]
    # Set by ask_final_confirmation; checked by _route_from_start together
    # with is_confirmation on the customer's NEXT raw reply — the one
    # deterministic gate in the whole pipeline.
    awaiting_final_yes: Optional[bool]


def _route_from_start(state: ConciergeState) -> str:
    stage = state.get("stage")
    if stage == "with_gift_picker":
        return "gift_picker"
    if stage == "checkout_info":
        return "checkout_info_agent"
    if stage == "confirm":
        return "confirm_agent"
    return "intent_router"


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
    """Structural reset, not left to the model to remember — reused for
    every entry into the Gift-Picker, whether a fresh gift_request or a
    mid-checkout hand-back: product_suggestions/cart_confirmed are stale
    from any earlier turn by the time we get here, and cart_snapshot
    captures the pre-turn cart for _route_after_gift_picker's diff below.
    """
    return {
        "product_suggestions": [],
        "cart_snapshot": state.get("cart"),
        "cart_confirmed": False,
    }


def _handoff_to_gift_picker(state: ConciergeState) -> dict:
    """Entry into the Gift-Picker from a MID-CHECKOUT hand-back
    (request_cart_revision, from the Checkout Info or Confirm agent), same
    turn as whichever agent called it — distinct from _reset_before_gift_picker,
    which handles the fresh-gift_request entry (from Intent Router) and
    does NOT need what this does.

    A synthetic system-note HumanMessage is required here, not optional:
    the calling agent's own final reply already closed out THIS turn's
    shared `messages` on an assistant turn (its normal ReAct-loop reply
    after calling request_cart_revision), and Gemini refuses a request
    that doesn't end on a user message or a function response — the exact
    bug already documented and fixed once for the Intent Router (see this
    module's own docstring), now surfacing between two real conversational
    agents chained in the same turn instead of a classifier and an agent.
    **Found live, the same way the original bug was:** this class of issue
    only shows up once two real model calls actually chain within one
    turn, so it wasn't visible until the Checkout Info Agent was live-tested.
    """
    reason = state.get("handoff_reason") or "the customer needs something changed."
    note = HumanMessage(content=f"[System note — not from the customer: {reason}]")
    return {
        "messages": [note],
        "product_suggestions": [],
        "cart_snapshot": state.get("cart"),
        "cart_confirmed": False,
    }


def _route_after_gift_picker(state: ConciergeState) -> str:
    if state.get("cart_confirmed"):
        return "enter_checkout_info"
    return "relay_gift_picker"


def _relay_gift_picker(state: ConciergeState) -> dict:
    """Sets stage by whether a cart currently exists, NOT by whether stage
    was already set going in — this node is reachable both from a fresh
    gift_request (stage never set) and from an already-active
    with_gift_picker/hand-back turn, and those two cases can't be told
    apart by "was stage None before" the way every other checkout agent's
    relay can (they're never entered from a stage-absent start). A cart
    existing means "still with_gift_picker, cart proposed but not yet
    approved" — matching the plan's own framing ("reached once a cart is
    proposed and awaiting approval"), including the very first proposal in
    a conversation, not just a revision. No cart (including right after
    cancel_checkout, which clears it) means back to plain Intent Router
    classification next turn, exactly like Phase 1-3.5's pre-cart phase
    always worked.
    """
    print("[console] product_suggestions:", state.get("product_suggestions"))
    return {
        "handoff_reason": None,
        "stage": "with_gift_picker" if state.get("cart") is not None else None,
    }


def _enter_checkout_info(state: ConciergeState) -> dict:
    """Same-turn transition from the Gift-Picker (cart just confirmed).
    Needs the same synthetic-note treatment _handoff_to_gift_picker uses,
    for the identical reason: the Gift-Picker's own final reply already
    closed this turn's shared `messages` on an assistant turn, and the
    Checkout Info Agent's own model call needs it to end on a user turn.
    """
    note = HumanMessage(
        content="[System note — not from the customer: the customer just approved this "
        "cart. Gather delivery city/date, recipient name/phone, delivery address, and "
        "sender name.]"
    )
    return {
        "messages": [note],
        "stage": "checkout_info",
        "handoff_reason": None,
        "checkout_info_finalized": False,
    }


def _route_after_checkout_info(state: ConciergeState) -> str:
    if state.get("stage") is None:
        return "relay_checkout_info"
    if state.get("handoff_reason"):
        return "handoff_to_gift_picker"
    if state.get("checkout_info_finalized"):
        return "enter_confirm"
    return "relay_checkout_info"


def _relay_checkout_info(state: ConciergeState) -> dict:
    update: dict = {}
    if state.get("stage") is not None:
        update["stage"] = "checkout_info"
    return update


def _enter_confirm(state: ConciergeState) -> dict:
    """Same-turn transition from the Checkout Info Agent (info just
    finalized) — same synthetic-note reasoning as _enter_checkout_info.
    """
    note = HumanMessage(
        content="[System note — not from the customer: checkout info is gathered. Show "
        "the order summary and work toward a final yes/no on placing the order.]"
    )
    return {"messages": [note], "stage": "confirm", "awaiting_final_yes": False}


def _route_after_confirm(state: ConciergeState) -> str:
    if state.get("stage") is None:
        # Either complete_order just ran and cleared everything (the
        # wrapper's fast path), or cancel_checkout fired inside the agent's
        # own loop (the slow path) — either way, nothing further to route.
        return END
    if state.get("handoff_reason"):
        return "handoff_to_gift_picker"
    return "relay_confirm"


def _relay_confirm(state: ConciergeState) -> dict:
    update: dict = {}
    if state.get("stage") is not None:
        update["stage"] = "confirm"
    return update


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

    confirm_agent_compiled = await build_confirm_agent()

    async def _run_confirm_agent(state: ConciergeState, config: RunnableConfig) -> dict:
        """Phase 3.7: the deterministic final-yes gate lives here now, as
        plain code that runs before the underlying compiled Confirm Agent
        is ever invoked — not a separate node/routing branch the way
        Phase 3.6 had it (`check_final_confirmation` /
        `_route_after_final_confirmation_check`, both retired). See this
        module's own docstring, hard-rule section, for the full reasoning.
        """
        if state.get("awaiting_final_yes"):
            reply = state["messages"][-1].text.strip()
            if is_confirmation(reply):
                phone_number = config["configurable"]["thread_id"]
                return await complete_order(state["cart"], state["checkout_info"], phone_number)
            # Not a clear yes — clear the gate, then fall through to the
            # real agent in this same call, no extra graph hop needed.
            state = {**state, "awaiting_final_yes": False}
        return await confirm_agent_compiled.ainvoke(state, config)

    graph.add_node("intent_router", _run_intent_router)
    graph.add_node("extract_intent", _extract_intent)
    graph.add_node("chitchat_node", _chitchat_node)
    graph.add_node("out_of_scope_node", _out_of_scope_node)
    graph.add_node("reset_before_gift_picker", _reset_before_gift_picker)
    graph.add_node("handoff_to_gift_picker", _handoff_to_gift_picker)
    graph.add_node("gift_picker", await build_gift_picker_agent())
    graph.add_node("relay_gift_picker", _relay_gift_picker)
    graph.add_node("enter_checkout_info", _enter_checkout_info)
    graph.add_node("checkout_info_agent", await build_checkout_info_agent())
    graph.add_node("relay_checkout_info", _relay_checkout_info)
    graph.add_node("enter_confirm", _enter_confirm)
    graph.add_node("confirm_agent", _run_confirm_agent)
    graph.add_node("relay_confirm", _relay_confirm)
    graph.add_node("track_order_stub", _track_order_stub)
    graph.add_node("return_item_stub", _return_item_stub)

    graph.add_conditional_edges(START, _route_from_start)
    graph.add_edge("intent_router", "extract_intent")
    graph.add_conditional_edges("extract_intent", _route_on_intent)
    graph.add_edge("chitchat_node", END)
    graph.add_edge("out_of_scope_node", END)
    graph.add_edge("reset_before_gift_picker", "gift_picker")
    graph.add_edge("handoff_to_gift_picker", "gift_picker")
    graph.add_conditional_edges("gift_picker", _route_after_gift_picker)
    graph.add_edge("relay_gift_picker", END)
    graph.add_edge("enter_checkout_info", "checkout_info_agent")
    graph.add_conditional_edges("checkout_info_agent", _route_after_checkout_info)
    graph.add_edge("relay_checkout_info", END)
    graph.add_edge("enter_confirm", "confirm_agent")
    graph.add_conditional_edges("confirm_agent", _route_after_confirm)
    graph.add_edge("relay_confirm", END)
    graph.add_edge("track_order_stub", END)
    graph.add_edge("return_item_stub", END)

    checkpointer = await get_checkpointer()
    return graph.compile(checkpointer=checkpointer, name="concierge_orchestrator")
