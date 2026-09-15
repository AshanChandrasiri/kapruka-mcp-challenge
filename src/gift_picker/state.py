"""Shared state shape between the Gift-Picker agent and the orchestrator.

`product_suggestions`/`cart` are declared here once and reused by both
`build_gift_picker_agent`'s own state schema and the orchestrator's
`ConciergeState` — matching field names/types is what lets LangGraph flow
them up automatically when the compiled agent is embedded as a subgraph
node, the same mechanism Phase 1 relies on for `messages`.
"""

from typing import NotRequired, Optional, TypedDict

from langchain.agents.middleware.types import AgentState


class Cart(TypedDict):
    items: list[dict]
    estimated_total: float
    notes: str
    delivery_city: Optional[str]
    delivery_date: Optional[str]


class GiftPickerState(AgentState):
    product_suggestions: NotRequired[list[dict]]
    cart: NotRequired[Optional[Cart]]
