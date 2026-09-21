"""Deterministic checkout state machine: Check Delivery -> Show Summary ->
Human Confirm -> Checkout -> Track Order. No LLM judgment anywhere in this
module except one deliberate exception: `_invoke_gift_picker_for_revision`
re-invokes the *same* Gift-Picker sub-agent to resolve a delivery-check
failure, an unclear confirm-step reply, or (Phase 3.5) a Checkout Router
`modify_request`/`unrelated` classification — all three funnel through the
shared `_hand_back_to_gift_picker` helper below, since all are really "hand
it back to the agent that knows how to build a cart."

Stage values (`ConciergeState["stage"]`), checked by the orchestrator
*before* the Intent Router runs:
- `collecting_delivery` — one field per turn, no LLM call to accept the
  field itself (Phase 3.5: the orchestrator's Checkout Router runs first to
  decide whether this reply is even an answer to the pending field at all).
- `resolving_delivery_conflict` — Gift-Picker is mid-revision and didn't
  produce a new cart this turn (asked the customer something instead); the
  next reply should go straight back to it, not through classification.
- `awaiting_confirm` — summary shown, waiting on an explicit yes/no.
- absent — normal flow (Intent Router runs as usual).

Phase 3.5: `handle_collecting_delivery` and `handle_awaiting_confirm` are
now only reached (for `collecting_delivery`/`awaiting_confirm` stages) after
the orchestrator's Checkout Router has classified the reply as
`answers_pending` — see src/orchestrator.py's `_route_on_checkout_intent`.
`handle_collecting_delivery` trusts that and reads `extracted_value` (the
value the router pulled out of free text) rather than the raw message.
`handle_awaiting_confirm` does NOT extend that trust to the one real
financial action in this codebase: `_is_confirmation`'s deterministic
keyword check still independently re-checks the customer's raw text every
time, regardless of what the router classified this reply as.

Hard rule: kapruka_create_order is only ever called from
handle_awaiting_confirm, after _is_confirmation passes. Never call it
anywhere else, and never skip that check "to save a round trip."
"""

from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from src.checkout.delivery import check_delivery_for_cart, resolve_city
from src.checkout.order import create_order, save_order, track_order_once
from src.checkout.summary import build_summary
from src.gift_picker.agent import build_gift_picker_agent
from src.gift_picker.state import Cart

MAX_REVISION_ATTEMPTS = 2

DATE_FORMAT_HINT = "Please use the format YYYY-MM-DD (e.g. 2026-09-25)."

FIELD_PROMPTS = {
    "delivery_city": lambda info: "Which city should I deliver this to?",
    "delivery_city_confirm": lambda info: (
        f"Just to confirm — did you mean **{info['_pending_city']}**? (yes/no)"
    ),
    "delivery_date": lambda info: f"What date would you like this delivered? {DATE_FORMAT_HINT}",
    "recipient_name": lambda info: "Who's this for — what name should I put on the delivery?",
    "recipient_phone": lambda info: (
        f"What's a good phone number for {info.get('recipient_name', 'the recipient')}, "
        "in case the courier needs to reach them?"
    ),
    "delivery_address": lambda info: (
        f"What's the delivery address in {info.get('delivery_city', 'that city')}?"
    ),
    "sender_name": lambda info: "Lastly, who should I put down as the sender (who this gift is from)?",
}

_CONFIRM_KEYWORDS = {
    "yes", "yep", "yeah", "yup", "confirm", "confirmed", "go ahead", "goahead",
    "place the order", "place order", "sounds good", "ok", "okay", "sure", "do it",
}


def _is_confirmation(text: str) -> bool:
    normalized = text.strip().lower().rstrip(".!")
    return normalized in _CONFIRM_KEYWORDS or normalized.startswith("yes")


def _invalid_date_reason(text: str) -> str | None:
    """Format/calendar-validity check only — deliberately not a
    deliverability check (that's kapruka_check_delivery's job, run later in
    Gate 2). Returns an error message to show the customer, or None if the
    date is well-formed.
    """
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return f"'{text}' isn't a valid date. {DATE_FORMAT_HINT}"
    return None


async def _invoke_gift_picker_for_revision(messages: list, cart: Cart, note_text: str) -> dict:
    """The one exception to "no agent loop" in this pipeline: hands a
    problem back to a fresh Gift-Picker instance, framed as a system note
    (not from the customer) so it has an actual conversational turn to
    react to. Returns the new messages to append (note included) and
    whatever cart/product_suggestions resulted — compare the returned
    `cart` against the one passed in to tell whether it actually revised.
    """
    note = HumanMessage(content=f"[System note — not from the customer: {note_text}]")
    input_messages = [*messages, note]
    agent = await build_gift_picker_agent()
    result = await agent.ainvoke({"messages": input_messages, "cart": cart, "product_suggestions": []})
    new_messages = [note, *result["messages"][len(input_messages):]]
    return {
        "new_messages": new_messages,
        "cart": result.get("cart"),
        "product_suggestions": result.get("product_suggestions", []),
    }


async def _hand_back_to_gift_picker(state: dict, note_text: str) -> dict:
    """Shared tail for every "this reply isn't progressing the pending
    checkout step" case: an unclear confirm-step reply (Phase 3, still used
    as handle_awaiting_confirm's own safety net when the Checkout Router
    says answers_pending but _is_confirmation disagrees) and a Checkout
    Router modify_request/unrelated classification during collecting_delivery
    or awaiting_confirm (Phase 3.5, handle_checkout_digression below).

    Gift-Picker re-proposed a cart -> feed it straight back into the
    checkout gates. Gift-Picker asked the customer something instead ->
    resolving_delivery_conflict, same as every other "hand-back" case.
    """
    cart = state["cart"]
    checkout_info = state.get("checkout_info") or {}
    revision = await _invoke_gift_picker_for_revision(state["messages"], cart, note_text)

    if revision["cart"] == cart:
        return {
            "messages": revision["new_messages"],
            "product_suggestions": revision["product_suggestions"],
            "stage": "resolving_delivery_conflict",
            "collecting_field": None,
        }

    result = await _advance_checkout(
        revision["cart"], checkout_info, state["messages"] + revision["new_messages"]
    )
    result["messages"] = [*revision["new_messages"], *result.get("messages", [])]
    return result


def _ask(cart: Cart, checkout_info: dict, key: str, extra_messages: list) -> dict:
    return {
        "cart": cart,
        "checkout_info": checkout_info,
        "stage": "collecting_delivery",
        "collecting_field": key,
        "messages": [*extra_messages, AIMessage(content=FIELD_PROMPTS[key](checkout_info))],
    }


async def _advance_checkout(cart: Cart, checkout_info: dict, messages: list) -> dict:
    checkout_info = dict(checkout_info)
    checkout_info.setdefault("delivery_city", cart.get("delivery_city"))
    checkout_info.setdefault("delivery_date", cart.get("delivery_date"))
    extra_messages: list = []

    # Gate 1: need a city + date before a delivery check is even possible.
    for key in ("delivery_city", "delivery_date"):
        if not checkout_info.get(key):
            return _ask(cart, checkout_info, key, extra_messages)

    # Gate 2: delivery check, once per checkout episode, with bounded
    # Gift-Picker-revision retries on failure. Keyed to the actual cart that
    # was checked (not just a bare boolean) so it self-invalidates whenever
    # the cart changes underneath it — e.g. a revision from the
    # resolving_delivery_conflict re-entry path, not only the
    # handle_awaiting_confirm one that used to be the only call site
    # remembering to reset this by hand.
    if not (checkout_info.get("delivery_checked") and checkout_info.get("delivery_checked_cart") == cart):
        attempts = 0
        while True:
            result = await check_delivery_for_cart(
                cart, checkout_info["delivery_city"], checkout_info["delivery_date"]
            )
            if result.ok:
                checkout_info["delivery_checked"] = True
                checkout_info["delivery_checked_cart"] = cart
                checkout_info["delivery_fee"] = result.fee
                checkout_info["perishable_warnings"] = result.perishable_warnings
                break

            attempts += 1
            if attempts > MAX_REVISION_ATTEMPTS:
                extra_messages.append(
                    AIMessage(
                        content=(
                            "I couldn't find a combination of these items that can be "
                            f"delivered to {checkout_info['delivery_city']} on "
                            f"{checkout_info['delivery_date']}. Could you suggest different "
                            "items, or a different city or date?"
                        )
                    )
                )
                return {
                    "messages": extra_messages,
                    "cart": cart,
                    "checkout_info": checkout_info,
                    "stage": "resolving_delivery_conflict",
                    "collecting_field": None,
                }

            failure_text = "; ".join(f"{i['name']} ({i['reason']})" for i in result.failed_items)
            note = (
                f"The following item(s) can't be delivered to {checkout_info['delivery_city']} "
                f"on {checkout_info['delivery_date']}: {failure_text}. Suggest alternative "
                "product(s) that ARE deliverable there, or ask the customer to pick a "
                "different city or date."
            )
            revision = await _invoke_gift_picker_for_revision(messages + extra_messages, cart, note)
            extra_messages.extend(revision["new_messages"])

            if revision["cart"] == cart:
                # Gift-Picker didn't re-propose — it's asking the customer
                # something instead of self-correcting.
                return {
                    "messages": extra_messages,
                    "cart": cart,
                    "checkout_info": checkout_info,
                    "stage": "resolving_delivery_conflict",
                    "collecting_field": None,
                    "product_suggestions": revision["product_suggestions"],
                }

            cart = revision["cart"]
            checkout_info["delivery_city"] = cart.get("delivery_city") or checkout_info["delivery_city"]
            checkout_info["delivery_date"] = cart.get("delivery_date") or checkout_info["delivery_date"]
            # loop: re-check the new cart

    # Gate 3: the rest of kapruka_create_order's required fields.
    for key in ("recipient_name", "recipient_phone", "delivery_address", "sender_name"):
        if not checkout_info.get(key):
            return _ask(cart, checkout_info, key, extra_messages)

    summary = build_summary(cart, checkout_info)
    return {
        "messages": [*extra_messages, AIMessage(content=summary)],
        "cart": cart,
        "checkout_info": checkout_info,
        "stage": "awaiting_confirm",
        "collecting_field": None,
    }


async def start_checkout_node(state: dict) -> dict:
    """Entry point when a (possibly revised) cart was just proposed this
    turn — reached from the orchestrator's cart-changed branch, whether
    that's a fresh gift_request or a resolving_delivery_conflict re-entry.
    """
    return await _advance_checkout(state["cart"], state.get("checkout_info") or {}, state["messages"])


async def handle_collecting_delivery(state: dict) -> dict:
    """One field per turn, no LLM call to accept the value itself — the
    orchestrator's Checkout Router has already classified this reply as
    answers_pending (Phase 3.5) before routing here, so `extracted_value`
    (the value it pulled from free text, e.g. "yeah Colombo 05" ->
    "Colombo 05") is used in place of the raw message where available,
    falling back to the raw text if this node is ever reached without one.
    `delivery_city`/`delivery_date` still get a deterministic validation
    pass against that answer BEFORE it's accepted into checkout_info, since
    those two feed straight into kapruka_check_delivery (Gate 2) and a typo
    there would otherwise surface as a confusing delivery-check failure
    instead of a simple "that's not a real city/date" reply.
    """
    field = state["collecting_field"]
    answer = (state.get("extracted_value") or state["messages"][-1].text).strip()
    cart = state["cart"]
    checkout_info = dict(state.get("checkout_info") or {})

    if field == "delivery_city_confirm":
        pending_city = checkout_info.pop("_pending_city", None)
        if _is_confirmation(answer):
            checkout_info["delivery_city"] = pending_city
            return await _advance_checkout(cart, checkout_info, state["messages"])
        return _ask(
            cart, checkout_info, "delivery_city",
            [AIMessage(content="No problem — which city should I deliver this to?")],
        )

    if field == "delivery_city":
        resolution = await resolve_city(answer)

        if resolution.status == "exact":
            checkout_info["delivery_city"] = resolution.canonical
            return await _advance_checkout(cart, checkout_info, state["messages"])

        if resolution.status == "alias":
            checkout_info["_pending_city"] = resolution.canonical
            return _ask(cart, checkout_info, "delivery_city_confirm", [])

        if resolution.status == "suggestions":
            options = ", ".join(resolution.candidates)
            return _ask(
                cart, checkout_info, "delivery_city",
                [AIMessage(content=f"I couldn't find '{answer}' exactly. Did you mean one of: {options}?")],
            )

        # no_match
        return _ask(
            cart, checkout_info, "delivery_city",
            [AIMessage(content=f"Sorry, '{answer}' doesn't look like a city we deliver to. Could you check the spelling and try again?")],
        )

    if field == "delivery_date":
        error = _invalid_date_reason(answer)
        if error:
            return _ask(cart, checkout_info, "delivery_date", [AIMessage(content=error)])
        checkout_info["delivery_date"] = answer
        return await _advance_checkout(cart, checkout_info, state["messages"])

    checkout_info[field] = answer
    return await _advance_checkout(cart, checkout_info, state["messages"])


async def handle_awaiting_confirm(state: dict, config: RunnableConfig) -> dict:
    reply = state["messages"][-1].text.strip()
    cart = state["cart"]
    checkout_info = state["checkout_info"]

    if _is_confirmation(reply):
        order_result = await create_order(cart, checkout_info)
        phone_number = config["configurable"]["thread_id"]
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
            "collecting_field": None,
            "product_suggestions": [],
        }

    # Safety net: the Checkout Router (Phase 3.5) classified this reply as
    # answers_pending before routing here, but that's a routing decision,
    # not a confirmation — _is_confirmation above is the only real gate on
    # create_order, and it just failed, so this reply still gets the same
    # hand-back-to-Gift-Picker treatment as any other non-yes.
    note = (
        f"The customer's reply during order confirmation wasn't a clear yes: '{reply}'. "
        "Figure out what they actually want — they might want to change an item, cancel, "
        "or ask a question — and call propose_cart again if the cart should change."
    )
    return await _hand_back_to_gift_picker(state, note)


async def handle_checkout_digression(state: dict) -> dict:
    """Phase 3.5: the Checkout Router classified this reply as
    modify_request or unrelated — it isn't an attempt to answer the pending
    step, so route it here instead of letting handle_collecting_delivery
    swallow it as a literal field value (the bug this phase exists to fix)
    or letting handle_awaiting_confirm's _is_confirmation silently no-op on
    it. Reuses the exact same hand-back mechanism as an unclear confirm
    reply or a failed delivery check — all three are "the agent that knows
    how to build a cart should look at this, not the field-collection code."
    """
    stage = state["stage"]
    if stage == "awaiting_confirm":
        note = (
            "The customer's reply during order confirmation wasn't a clear yes. "
            "Figure out what they actually want — they might want to change an "
            "item, cancel, or ask a question — and call propose_cart again if the "
            "cart should change."
        )
    else:
        field = state.get("collecting_field")
        prompt_fn = FIELD_PROMPTS.get(field)
        pending_question = prompt_fn(state.get("checkout_info") or {}) if prompt_fn else "a checkout detail"
        note = (
            f'The customer\'s reply didn\'t answer what was just asked ("{pending_question}") '
            "— they might want to change an item, cancel, or ask about something else. "
            "Figure out what they actually want, and call propose_cart again if the cart "
            "should change."
        )
    return await _hand_back_to_gift_picker(state, note)


def cancel_checkout_node(state: dict) -> dict:
    """Phase 3.5: the Checkout Router's cancel_checkout classification —
    structural clear, no LLM call (the classification itself was the only
    judgment call needed here).
    """
    return {
        "messages": [
            AIMessage(
                content="No problem — I've cancelled this checkout. Let me know if "
                "you'd like to start a fresh gift search."
            )
        ],
        "cart": None,
        "checkout_info": None,
        "stage": None,
        "collecting_field": None,
    }
