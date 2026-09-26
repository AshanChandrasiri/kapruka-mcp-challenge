"""Checkout Info Agent — Phase 3.6. Replaces Phase 3's deterministic
one-field-per-turn Gate 1/2/3 machinery (`_advance_checkout`,
`handle_collecting_delivery`, `_ask`, `FIELD_PROMPTS`) with a real ReAct
agent, same class as the Gift-Picker: it gathers everything
`kapruka_create_order` needs besides the cart itself — delivery city,
delivery date, recipient name/phone, delivery address, sender name — and
validates deliverability itself instead of a hard-coded gate order.

Renamed from an earlier "Delivery Agent" idea during design — it covers
all three of Phase 3's old gates now, not just the delivery leg, so
"Delivery Agent" undersold it.

Embedded as a literal, always-on subgraph node (same mechanism as the
Gift-Picker) — `dynamic_prompt` middleware carries this turn's cart/
checkout-info-so-far/today's-date into the system prompt at actual
model-call time, and `InjectedState` lets `check_delivery`/
`finalize_checkout_info` read the live cart/checkout_info without asking
the model to retype them as tool arguments (and, critically, without a
per-turn-rebuild's closure going stale if both tools fire within the same
ReAct loop).

No lookup/autofill for the new fields — checked `src/db/schema.sql`
directly rather than assumed: `recipients` only stores the customer's own
name/relationship/preferences, nothing about a recipient's phone or a
delivery address. So recipient_phone/delivery_address are plain free text,
same posture Phase 3's Gate 3 had.
"""

from datetime import datetime
from typing import Annotated, NotRequired, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt
from langchain.agents.middleware.types import AgentState, ModelRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from src.checkout.delivery import check_delivery_for_cart
from src.checkout.delivery import resolve_city as _resolve_city_lookup
from src.checkout.shared_tools import cancel_checkout, request_cart_revision
from src.config import GOOGLE_API_KEY, LLM_MODEL
from src.gift_picker.state import Cart
from src.prompts import CHECKOUT_INFO_AGENT_INSTRUCTIONS

DATE_FORMAT_HINT = "Resolve it to the format YYYY-MM-DD (e.g. 2026-09-25) before calling this again."


class CheckoutInfoState(AgentState):
    cart: NotRequired[Optional[Cart]]
    checkout_info: NotRequired[Optional[dict]]
    # Set by finalize_checkout_info — the orchestrator's own signal for
    # "did this agent finish this turn," same role cart_confirmed plays
    # for the Gift-Picker. Reset structurally by the orchestrator before
    # this node runs (never trust "all six fields already present" alone —
    # a REVISED cart re-entering this stage would already have stale
    # fields left over from before the revision).
    checkout_info_finalized: NotRequired[bool]
    handoff_reason: NotRequired[Optional[str]]
    stage: NotRequired[Optional[str]]


def _invalid_delivery_date_reason(date_str: str) -> Optional[str]:
    """Independent, ground-truth guard — adapted from Phase 3's
    `_invalid_date_reason`, now also requiring at least one day ahead of
    today. The agent (not a fixed prompt) resolves relative dates like
    "next thursday" itself, so its arithmetic isn't trusted blindly here.
    """
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return f"'{date_str}' isn't a valid YYYY-MM-DD date. {DATE_FORMAT_HINT}"
    if parsed <= datetime.now().date():
        return f"'{date_str}' must be at least one day after today ({datetime.now().date().isoformat()}). {DATE_FORMAT_HINT}"
    return None


def _summarize_cart(cart: Optional[Cart]) -> str:
    cart = cart or {}
    items = ", ".join(
        item.get("name", item.get("product_id", "?")) for item in cart.get("items", [])
    ) or "(no items)"
    return f"items=[{items}], estimated_total={cart.get('estimated_total')}"


def _summarize_checkout_info(checkout_info: Optional[dict]) -> str:
    info = checkout_info or {}
    visible = {k: v for k, v in info.items() if v and k != "perishable_warnings"}
    return str(visible) if visible else "(nothing yet)"


@tool
async def resolve_city(query: str) -> dict:
    """Look up a customer-stated delivery city against Kapruka's real
    delivery network before using it in check_delivery. Returns:
    {"status": "exact", "canonical": "<name>"} — use canonical as-is.
    {"status": "alias", "canonical": "<name>"} — a known alias/vernacular
    spelling; confirm with the customer before using canonical.
    {"status": "suggestions", "candidates": [...]} — no exact/alias match,
    offer these back. {"status": "no_match"} — nothing resembling this
    exists; ask the customer to check the spelling.
    """
    resolution = await _resolve_city_lookup(query)
    return {
        "status": resolution.status,
        "canonical": resolution.canonical,
        "candidates": resolution.candidates,
    }


@tool
async def check_delivery(
    city: str,
    date: str,
    cart: Annotated[Optional[dict], InjectedState("cart")] = None,
    checkout_info: Annotated[Optional[dict], InjectedState("checkout_info")] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = None,
) -> Command:
    """Check whether the CURRENT cart can be delivered to `city` on
    `date` (YYYY-MM-DD — resolve any relative date yourself first, using
    today's date from your system prompt). Call this before asking about
    recipient/address/sender, and again if the city or date changes. On
    success, the delivery fee and any perishable warnings are recorded
    automatically — you don't need to repeat them via
    finalize_checkout_info.
    """
    date_error = _invalid_delivery_date_reason(date)
    if date_error:
        return Command(update={"messages": [ToolMessage(date_error, tool_call_id=tool_call_id)]})

    result = await check_delivery_for_cart(cart or {"items": []}, city, date)
    if not result.ok:
        failure_text = "; ".join(f"{i['name']} ({i['reason']})" for i in result.failed_items)
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Not deliverable to {city} on {date}: {failure_text}. Consider "
                        "request_cart_revision if no city/date change will fix this.",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    updated_info = {
        **(checkout_info or {}),
        "delivery_city": city,
        "delivery_date": date,
        "delivery_fee": result.fee,
        "perishable_warnings": result.perishable_warnings,
    }
    warning_text = f" Note: {result.perishable_warnings}" if result.perishable_warnings else ""
    return Command(
        update={
            "checkout_info": updated_info,
            "messages": [
                ToolMessage(
                    f"Deliverable to {city} on {date}. Delivery fee: LKR {result.fee}.{warning_text}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def finalize_checkout_info(
    city: str,
    date: str,
    recipient_name: str,
    recipient_phone: str,
    delivery_address: str,
    sender_name: str,
    checkout_info: Annotated[Optional[dict], InjectedState("checkout_info")] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = None,
) -> Command:
    """Finalize everything needed to place the order. Only meaningfully
    callable once check_delivery has passed for city/date AND every field
    below is actually gathered from the customer (not guessed). Merges
    with whatever check_delivery already recorded (fee, perishable
    warnings) rather than overwriting it. Ends your turn.
    """
    merged = {
        **(checkout_info or {}),
        "delivery_city": city,
        "delivery_date": date,
        "recipient_name": recipient_name,
        "recipient_phone": recipient_phone,
        "delivery_address": delivery_address,
        "sender_name": sender_name,
    }
    return Command(
        update={
            "checkout_info": merged,
            "checkout_info_finalized": True,
            "messages": [ToolMessage("Checkout info finalized.", tool_call_id=tool_call_id)],
        }
    )


@dynamic_prompt
def _checkout_info_prompt(request: ModelRequest) -> str:
    return CHECKOUT_INFO_AGENT_INSTRUCTIONS.format(
        cart_summary=_summarize_cart(request.state.get("cart")),
        checkout_info_summary=_summarize_checkout_info(request.state.get("checkout_info")),
        today=datetime.now().date().isoformat(),
    )


async def build_checkout_info_agent():
    """Fresh agent instance — same "can't be reused across parents"
    reasoning as build_gift_picker_agent/build_router_agent.
    """
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, google_api_key=GOOGLE_API_KEY)
    return create_agent(
        llm,
        tools=[resolve_city, check_delivery, finalize_checkout_info, request_cart_revision, cancel_checkout],
        middleware=[_checkout_info_prompt],
        state_schema=CheckoutInfoState,
    )
