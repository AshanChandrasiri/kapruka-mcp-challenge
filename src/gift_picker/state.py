"""Shared state shape between the Gift-Picker agent and the orchestrator.

`product_suggestions`/`cart` are declared here once and reused by both
`build_gift_picker_agent`'s own state schema and the orchestrator's
`ConciergeState` — matching field names/types is what lets LangGraph flow
them up automatically when the compiled agent is embedded as a subgraph
node, the same mechanism Phase 1 relies on for `messages`.
"""

from typing import NotRequired, Optional, TypedDict

from langchain.agents.middleware.types import AgentState
from pydantic import BaseModel


class ProductSuggestion(BaseModel):
    product_id: str
    name: str
    price: float
    image_url: str
    url: str


class Cart(TypedDict):
    items: list[dict]
    estimated_total: float
    notes: str


class GiftPickerState(AgentState):
    product_suggestions: NotRequired[list[ProductSuggestion]]
    cart: NotRequired[Optional[Cart]]
    # Phase 3.6: set by confirm_cart_and_proceed (the customer approved the
    # CURRENT cart, not just proposed one) — the orchestrator's own
    # cart-diff detection can't tell "new cart" from "approved cart" apart
    # on its own, so this is a second, explicit signal.
    cart_confirmed: NotRequired[bool]
    # Phase 3.6: non-null while the Gift-Picker is being consulted
    # mid-checkout (set by request_cart_revision, a tool shared with the
    # Checkout Info/Confirm agents) — read by build_gift_picker_agent's
    # dynamic system prompt, cleared by the orchestrator after this node
    # runs. Absent/None on a fresh gift_request.
    handoff_reason: NotRequired[Optional[str]]
    # Declared here (not just used) so the shared cancel_checkout tool's
    # Command update reaches the PARENT ConciergeState when the Gift-Picker
    # itself calls it — a child subgraph node only projects channels its
    # own state schema declares; without these two, clearing them here
    # would silently no-op instead of clearing the parent's copy.
    stage: NotRequired[Optional[str]]
    checkout_info: NotRequired[Optional[dict]]
