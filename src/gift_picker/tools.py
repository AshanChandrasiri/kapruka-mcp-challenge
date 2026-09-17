"""Custom tools for the Gift-Picker agent (alongside the three scoped MCP
tools — search_products/get_product/list_categories — wired in agent.py).
"""

from typing import Annotated, Optional

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool

from langgraph.types import Command

from src.db.recipients import find_recipients
from src.gift_picker.state import Cart, ProductSuggestion


@tool
async def get_recipient_profile(recipient_name: str, config: RunnableConfig) -> dict:
    """Look up the customer's saved info about a recipient by name or
    relationship (e.g. "mom", "my brother Kasun", "husband").

    Call this BEFORE asking the customer clarifying questions about someone
    they've named — they may have already told this concierge (on a past
    order) what that person likes, their budget range, etc, and re-asking
    for it is annoying.

    Returns {"matches": [...]} — zero, one, or several recipient records
    (each with name/relationship/preferences/budget_min/budget_max). Do not
    assume the first match is right: if there's more than one, or the
    query is a loose alias (e.g. "mom" against a stored relationship of
    "mother"), use judgement or ask the customer to disambiguate rather
    than guessing.
    """
    phone_number = config["configurable"]["thread_id"]
    matches = await find_recipients(phone_number, recipient_name)
    return {"matches": matches}


@tool
def suggest_products(
    products: list[ProductSuggestion],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Show the customer a set of candidate products to react to, before you've
    settled on a final cart.

    Call this before describing specific products by name in your reply —
    the customer sees these as cards alongside your text, so your narration
    should match what's actually listed here.

    Each product needs:
    - `product_id`: Kapruka's search_products/get_product tools return this
      field as `id` — rename it, and copy the value EXACTLY as returned
      (same case, same characters, do not retype it).
    - `name`
    - `price`: a plain number — if the source has `price.amount`/
      `price.currency`, flatten it to just the amount.
    - `image_url`
    - `url`: the product's page link on Kapruka, as returned by
      kapruka_get_product.

    Each call REPLACES the previous suggestions, it doesn't add to them —
    call it again with the full new set whenever you refine the options.
    """
    return Command(
        update={
            "product_suggestions": products,
            "messages": [
                ToolMessage(
                    f"Suggested {len(products)} product(s) to the customer.",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def propose_cart(
    items: list[dict],
    estimated_total: float,
    tool_call_id: Annotated[str, InjectedToolCallId],
    notes: str = "",
    delivery_city: Optional[str] = None,
    delivery_date: Optional[str] = None,
) -> Command:
    """Finalize the gift as a concrete cart, ready for checkout.

    Call this ONLY once you and the customer have converged on specific
    items — not to tentatively summarize progress, and not just because a
    turn is ending. If you're still narrowing down options, use
    suggest_products and ask a follow-up instead. Calling this ends your
    turn: don't call it and then keep negotiating in the same reply.

    Each item dict must use the key `product_id` (NOT `id` — see
    suggest_products for the exact-case, no-retyping rule), plus a plain
    `price` number (never a nested price object) and whatever name info you
    have.

    Pass delivery_city/delivery_date only if the customer has already
    stated them in this conversation — never guess, and never ask for them
    just to fill in this call; a later step collects whatever's still
    missing.
    """
    cart: Cart = {
        "items": items,
        "estimated_total": estimated_total,
        "notes": notes,
        "delivery_city": delivery_city,
        "delivery_date": delivery_date,
    }
    return Command(
        update={
            "cart": cart,
            "product_suggestions": [],
            "messages": [
                ToolMessage(f"Cart proposed: {cart}", tool_call_id=tool_call_id)
            ],
        }
    )
