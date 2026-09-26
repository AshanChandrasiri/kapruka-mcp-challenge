"""Confirm Agent — Phase 3.6. Replaces `awaiting_confirm`/
`handle_awaiting_confirm`'s deterministic non-yes fallback with a real
ReAct agent that can actually engage with whatever the customer says while
the order summary is on the table — a question about the summary, a
request to change something, a vague non-answer — instead of every non-yes
reply routing through the same generic "hand back to the Gift-Picker" note
Phase 3 used regardless of what was actually said.

**This agent never has `kapruka_create_order` as a tool — verifiable by
inspection.** It's the seam between "fully agentic conversation" and "the
one deterministic gate" the whole checkout pipeline is built around:
`ask_final_confirmation` only ARMS the gate (sets `awaiting_final_yes`);
the actual trigger is `_is_confirmation` run directly on the customer's
raw reply by the orchestrator (`src/orchestrator.py`), with no agent call
involved in that specific check at all. See CLAUDE.md's hard rule.

Embedded as a literal, always-on subgraph node, same mechanism as the
Gift-Picker and Checkout Info Agent — `dynamic_prompt` injects the order
summary (`build_summary`, reused as-is from Phase 3) fresh every call, so
it always reflects the current cart/checkout_info even after a revision
round-trip.
"""

from typing import Annotated, NotRequired, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import dynamic_prompt
from langchain.agents.middleware.types import AgentState, ModelRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.types import Command

from src.checkout.shared_tools import cancel_checkout, request_cart_revision
from src.checkout.summary import build_summary
from src.config import GOOGLE_API_KEY, LLM_MODEL
from src.gift_picker.state import Cart
from src.prompts import CONFIRM_AGENT_INSTRUCTIONS


class ConfirmState(AgentState):
    cart: NotRequired[Optional[Cart]]
    checkout_info: NotRequired[Optional[dict]]
    handoff_reason: NotRequired[Optional[str]]
    stage: NotRequired[Optional[str]]
    # Set by ask_final_confirmation; the orchestrator's deterministic gate
    # (src/orchestrator.py) checks this alongside _is_confirmation on the
    # customer's NEXT raw reply — this agent never checks it itself and
    # never calls kapruka_create_order.
    awaiting_final_yes: NotRequired[bool]


@tool
def ask_final_confirmation(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Call when you're ready to ask the customer the final yes/no to
    place the order — after showing the summary and addressing anything
    they raised. This only arms the confirmation gate; it does NOT ask the
    customer anything by itself and does NOT place the order — your own
    reply right after calling this is what actually asks them clearly
    (e.g. "shall I place this order now? (yes/no)").
    """
    return Command(
        update={
            "awaiting_final_yes": True,
            "messages": [ToolMessage("Armed the final confirmation gate.", tool_call_id=tool_call_id)],
        }
    )


_SUMMARY_REQUIRED_FIELDS = (
    "delivery_city", "delivery_date", "recipient_name", "recipient_phone",
    "delivery_address", "sender_name",
)


@dynamic_prompt
def _confirm_prompt(request: ModelRequest) -> str:
    """Every entry INTO the confirm stage arrives with a fully-populated
    checkout_info (that's what checkout_info_finalized gates on) — but
    `dynamic_prompt` re-runs on EVERY model call within this agent's own
    ReAct loop, not just the first, and cancel_checkout can fire mid-loop:
    tool call clears checkout_info -> the SAME loop's next model call
    (generating the agent's own "okay, cancelled!" reply) would otherwise
    hit this callback with checkout_info already gone. **Found live**:
    build_summary assumes a complete dict (direct key access, no
    defaults) and raised a bare KeyError once that happened.
    """
    cart = request.state.get("cart")
    checkout_info = request.state.get("checkout_info")
    if not cart or not checkout_info or not all(checkout_info.get(k) for k in _SUMMARY_REQUIRED_FIELDS):
        order_summary = "(Checkout was just cancelled — nothing further to confirm.)"
    else:
        order_summary = build_summary(cart, checkout_info)
    return CONFIRM_AGENT_INSTRUCTIONS.format(order_summary=order_summary)


async def build_confirm_agent():
    """Fresh agent instance — same "can't be reused across parents"
    reasoning as build_gift_picker_agent/build_checkout_info_agent.
    """
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, google_api_key=GOOGLE_API_KEY)
    return create_agent(
        llm,
        tools=[ask_final_confirmation, request_cart_revision, cancel_checkout],
        middleware=[_confirm_prompt],
        state_schema=ConfirmState,
    )
