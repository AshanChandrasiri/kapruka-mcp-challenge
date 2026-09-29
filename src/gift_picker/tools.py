"""Custom tools for the Gift-Picker agent (alongside the three scoped MCP
tools — search_products/get_product/list_categories — wired in agent.py).
"""

from typing import Annotated

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
) -> Command:
    """Propose (or revise) the gift as a concrete cart, for the customer to
    react to — NOT the same as the customer approving it. Call this once
    you and the customer have converged on specific items, whether that's
    the first proposal or a change to one already on the table. If you're
    still narrowing down options, use suggest_products and ask a follow-up
    instead.

    Each item dict is ONE ROW PER DISTINCT PRODUCT (never repeat the same
    product_id as separate entries for multiple units — use `quantity`
    instead) and must include:
    - `product_id` (NOT `id` — see suggest_products for the exact-case,
      no-retyping rule)
    - `name`
    - `price`: a plain number per unit (never a nested price object)
    - `quantity`: a plain integer, defaulting to 1 if the customer didn't
      say otherwise — never split multiple units of the same product into
      separate items
    - `url` and `image_url`: copy these forward from whatever
      suggest_products/kapruka_get_product call you already made for this
      product this conversation — don't drop them just because a few turns
      passed since you looked the product up.

    Cart-only — no delivery_city/delivery_date here. Delivery details are
    gathered later, once the customer has actually approved this cart
    (confirm_cart_and_proceed), by a dedicated step that also validates
    deliverability.

    Calling this ends your turn — don't call it and then keep negotiating
    in the same reply. If the customer has ALREADY approved what's here
    with nothing further to change, call confirm_cart_and_proceed instead
    of re-proposing the same cart.
    """
    cart: Cart = {
        "items": items,
        "estimated_total": estimated_total,
        "notes": notes,
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


@tool
def confirm_cart_and_proceed(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Call when the customer approves the CURRENT cart with nothing
    further to change — the cart already on the table is what they want,
    and they're ready to move on to delivery/checkout details.

    Distinct from propose_cart: propose_cart is "here's a cart" (new or
    changed, still open to further changes); this is "yes, that one,
    let's proceed" (a judgment call about the customer's LATEST reply, not
    a restatement of the cart itself — don't pass any arguments, don't
    re-describe the cart, just call this).

    Do NOT call this just because a turn is ending, or to tentatively
    move things along — only when the customer's own words clearly
    approve what's already proposed with nothing left to negotiate.
    """
    return Command(
        update={
            "cart_confirmed": True,
            "messages": [
                ToolMessage("Cart confirmed by the customer; proceeding to checkout.", tool_call_id=tool_call_id)
            ],
        }
    )
