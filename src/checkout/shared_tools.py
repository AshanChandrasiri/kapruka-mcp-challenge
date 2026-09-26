"""Tools shared by all three checkout-time agents (Gift-Picker,
Checkout Info Agent, Confirm Agent) — kept in their own module rather than
`gift_picker/tools.py` since it isn't the Gift-Picker's own file anymore
once three different agents need these.

Both are `Command`-based and end whichever agent's turn called them, same
shape as `propose_cart`/`suggest_products`:

- `request_cart_revision(reason)` — "the cart itself needs to change, not
  a checkout-info field." Writes `handoff_reason`; the orchestrator (not
  the tool) is what actually routes to the Gift-Picker afterward — see
  src/orchestrator.py's conditional edge after each checkout-agent node.
  A subgraph tool returning `Command(graph=Command.PARENT)` was considered
  and rejected: untested in this codebase, and unnecessary — writing
  `handoff_reason` via a plain `Command(update=...)` and letting a parent
  conditional edge react to it reuses the exact mechanism `propose_cart`
  already proves works.
- `cancel_checkout()` — clears the whole checkout (`stage`/`cart`/
  `checkout_info`/`handoff_reason`), same as Phase 3.5's dedicated
  `cancel_checkout_node`, but now reachable from any of the three agents'
  own judgment instead of a router classification. Terminal: no hand-off
  needed, the calling agent's own next reply (informed by the tool's
  result) is what tells the customer it's cancelled.
"""

from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command


@tool
def request_cart_revision(
    reason: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Call when the CART itself needs to change — swap an item, add
    something, the customer rejected what's there, or a delivery check
    failed and an alternative product is needed — rather than a
    checkout-info field (city/date/recipient/address/sender) or a
    yes/no on placing the order.

    `reason` should describe concretely what needs to change so the
    Gift-Picker (who receives it) has enough context to act, e.g. "the
    cake in the cart can't be delivered to Jaffna on 2026-10-15; suggest a
    deliverable alternative or a different city/date."

    Ends your turn — control returns to the Gift-Picker.
    """
    return Command(
        update={
            "handoff_reason": reason,
            "messages": [
                ToolMessage(
                    f"Handed back to the Gift-Picker: {reason}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def cancel_checkout(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Call when the customer clearly wants to cancel/stop this checkout
    entirely — not a request to change an item or a field, an outright
    "never mind, cancel this."

    Clears the cart and everything gathered so far. After calling this,
    just acknowledge the cancellation to the customer in your own reply —
    there's nothing further to hand off to.
    """
    return Command(
        update={
            "stage": None,
            "cart": None,
            "checkout_info": None,
            "handoff_reason": None,
            "messages": [
                ToolMessage("Checkout cancelled; all checkout state cleared.", tool_call_id=tool_call_id)
            ],
        }
    )
