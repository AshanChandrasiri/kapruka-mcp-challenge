"""The one deterministic piece left in checkout, Phase 3.6: the literal
trigger for `kapruka_create_order`. Everything else Phase 3 put here
(`_advance_checkout`, `handle_collecting_delivery`, `_ask`, `FIELD_PROMPTS`,
`_invalid_date_reason`) and everything Phase 3.5 added
(`_invoke_gift_picker_for_revision`, `_hand_back_to_gift_picker`,
`handle_checkout_digression`, `cancel_checkout_node` as a router-reached
node) is retired — replaced by three real agents (Gift-Picker, extended;
Checkout Info Agent; Confirm Agent — see `src/gift_picker/agent.py`,
`src/checkout/checkout_info_agent.py`, `src/checkout/confirm_agent.py`)
that own their own judgment, wired directly into `src/orchestrator.py`.

Hard rule, unchanged since Phase 3: kapruka_create_order is only ever
called from `complete_order`, after `_is_confirmation` passes on the
customer's raw reply — no agent call involved in that specific check.
Never call it anywhere else, and never skip that check "to save a round
trip."
"""

from langchain_core.messages import AIMessage

from src.checkout.order import create_order, save_order, track_order_once
from src.gift_picker.state import Cart

_CONFIRM_KEYWORDS = {
    "yes", "yep", "yeah", "yup", "confirm", "confirmed", "go ahead", "goahead",
    "place the order", "place order", "sounds good", "ok", "okay", "sure", "do it",
}


def is_confirmation(text: str) -> bool:
    """The one deterministic gate on kapruka_create_order — public (not
    module-private) since src/orchestrator.py now calls this directly,
    outside any agent, as the literal trigger check. Verifiable by
    inspection: this function and complete_order below are the only two
    places in this codebase that combine to call kapruka_create_order.
    """
    normalized = text.strip().lower().rstrip(".!")
    return normalized in _CONFIRM_KEYWORDS or normalized.startswith("yes")


async def complete_order(cart: Cart, checkout_info: dict, phone_number: str) -> dict:
    """The one real financial action in this codebase. Only ever called by
    src/orchestrator.py's deterministic gate, after `_is_confirmation` has
    already passed directly on the customer's raw text — no agent/LLM
    judgment anywhere in this call path.
    """
    order_result = await create_order(cart, checkout_info)
    await save_order(phone_number, cart, checkout_info, order_result)
    await track_order_once(order_result.get("order_ref", ""))

    reply_text = (
        "Your order is ready! Complete payment here within 60 minutes: "
        f"{order_result.get('checkout_url')}\n\nOrder reference: {order_result.get('order_ref')}"
    )
    return {
        "messages": [AIMessage(content=reply_text)],
        "cart": None,
        "checkout_info": None,
        "stage": None,
        "handoff_reason": None,
        "awaiting_final_yes": None,
        "product_suggestions": [],
    }
