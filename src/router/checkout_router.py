"""Checkout Router — second structured-output classifier, no tools, no
loop. Same class as the Intent Router (LangGraph agent node, structured
output, zero tools attached), but scoped to turns where a checkout `stage`
is already set. Kept as its own module/prompt rather than growing the
Intent Router's 5-way schema — each prompt stays focused on one job:
cold-start intent vs. mid-checkout digression.

This classifier's primary context isn't the message transcript —
`stage`/`collecting_field`/a cart snapshot are serialized explicitly into
the prompt per call (see build_checkout_router_agent), since "does this
reply answer the pending question" is meaningless without knowing what the
pending question actually is. Full chat history is also passed in (both
from the orchestrator's live path and this module's own standalone path,
below) as working memory on top of that explicit snapshot — the same
reason the Gift-Picker gets full history — for cases where a short reply
is only disambiguated by something said earlier in the conversation; the
explicit stage/field/cart snapshot stays authoritative for what's actually
pending, history is context, not a replacement for it.
"""

from typing import Literal, Optional

from langchain.agents import create_agent
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from src.config import GOOGLE_API_KEY, LLM_MODEL
from src.prompts import CHECKOUT_ROUTER_INSTRUCTIONS
from src.session import get_checkpointer, session_identity

CheckoutIntent = Literal["answers_pending", "modify_request", "cancel_checkout", "unrelated"]


class CheckoutIntentClassification(BaseModel):
    intent: CheckoutIntent = Field(
        description="How the customer's latest reply relates to the pending checkout step."
    )
    extracted_value: Optional[str] = Field(
        default=None,
        description=(
            "Only when intent is answers_pending: the actual value pulled from the "
            "customer's free text (e.g. 'yeah ship it to Colombo 05' -> 'Colombo 05'; "
            "a plain yes/no during order confirmation passes through as-is). Null for "
            "every other intent."
        ),
    )


def build_checkout_router_agent(stage: str, collecting_field: Optional[str], cart_summary: str):
    """Fresh LangGraph agent node instance — no tools, structured output
    only. Same reuse constraint as build_router_agent (LangGraph agents
    can't be reused as a sub-agent of more than one parent), plus this one
    also needs a fresh system prompt every call since it's formatted with
    this turn's stage/collecting_field/cart snapshot.
    """
    if collecting_field:
        pending_desc = collecting_field
    elif stage == "awaiting_confirm":
        pending_desc = "(none — waiting on order confirmation: a yes/no)"
    else:
        # resolving_delivery_conflict: no single field is pending — the
        # Gift-Picker is mid-conversation with the customer about the cart
        # itself (e.g. suggesting a deliverable alternative).
        pending_desc = "(none — no specific field pending; the assistant is discussing the cart itself with the customer)"

    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, google_api_key=GOOGLE_API_KEY)
    system_prompt = CHECKOUT_ROUTER_INSTRUCTIONS.format(
        stage=stage,
        collecting_field=pending_desc,
        cart_summary=cart_summary,
    )
    return create_agent(
        llm,
        tools=[],
        system_prompt=system_prompt,
        response_format=CheckoutIntentClassification,
    )


async def classify_checkout_intent(
    phone_number: str,
    message: str,
    stage: str,
    collecting_field: Optional[str],
    cart_summary: str,
) -> CheckoutIntentClassification:
    """Standalone classification path, outside the orchestrator — mirrors
    classify_intent's role for isolated testing (scripts/test_checkout_router.py).

    Reads the FULL stored session history for phone_number, no bound —
    unlike classify_intent's standalone path, which deliberately bounds to
    the last few messages (a cost optimization specific to cold-start
    intent classification). This mirrors how the Gift-Picker always gets
    full history as working memory, and how the live orchestrator path
    (_run_checkout_router) now does too. On a phone_number with no stored
    session yet (e.g. each scripts/test_checkout_router.py case uses a
    fresh one), history is simply empty and this behaves exactly as before.
    """
    checkpointer = await get_checkpointer()
    checkpoint_tuple = await checkpointer.aget_tuple(session_identity(phone_number))
    history: list[AnyMessage] = []
    if checkpoint_tuple is not None:
        history = checkpoint_tuple.checkpoint.get("channel_values", {}).get("messages", [])

    agent = build_checkout_router_agent(stage, collecting_field, cart_summary)
    result = await agent.ainvoke({"messages": [*history, HumanMessage(content=message)]})
    return result["structured_response"]
