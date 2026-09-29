"""Intent Router — single structured-output LLM call, no tools, no loop.

Classifies the customer's latest message into one of five intents. Built as
a LangGraph agent node (response_format, zero tools) so it shares the same
primitives (compiled LangGraph + PostgresSaver checkpointer) as the
Gift-Picker agent — the "not agentic" part is behavioral, not a different
class: with zero tools attached there is nothing for it to call, so it can't
enter a ReAct loop no matter what runs it.
"""

from typing import Literal

from langchain.agents import create_agent
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from src.config import GOOGLE_API_KEY, LLM_MODEL
from src.prompts import INTENT_ROUTER_INSTRUCTIONS
from src.session import get_checkpointer, session_identity

Intent = Literal["gift_request", "track_order", "return_item", "chitchat", "out_of_scope"]

# Standalone/isolated-testing path only (classify_intent below) — bounded to
# this many of the most recent messages. LangGraph's own message-history
# handling turned out to be binary (full history via the checkpointer, or
# none) rather than a partial-N lever, so bounding is done by slicing the
# message list ourselves before invoking, not via any built-in config knob.
BOUNDED_HISTORY_LEN = 4


class IntentClassification(BaseModel):
    intent: Intent = Field(description="The single best-fitting intent for the customer's latest message.")


def build_router_agent():
    """Fresh LangGraph agent node instance — no tools, structured output only.

    LangGraph agents can't be reused as a sub-agent of more than one parent,
    so this is called fresh each time a router is embedded (by the
    orchestrator) or invoked standalone (by classify_intent).
    """
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, google_api_key=GOOGLE_API_KEY)
    return create_agent(
        llm,
        tools=[],
        system_prompt=INTENT_ROUTER_INSTRUCTIONS,
        response_format=IntentClassification,
    )


async def classify_intent(phone_number: str, message: str) -> IntentClassification:
    """Standalone classification path, outside the orchestrator.

    Used for isolated testing (scripts/test_intent_router.py) and anywhere
    the router runs outside the orchestrator. Reads up to the last
    BOUNDED_HISTORY_LEN messages from the shared session (for ambiguous
    messages that need prior context) but does not persist this call back
    to that session — it's a side read, not a turn.
    """
    checkpointer = await get_checkpointer()
    # Phase 4 decoupled thread_id from phone_number, but this standalone
    # path (isolated testing / non-orchestrator use, a side read not a
    # turn) never had a real thread_id to plumb through — it already
    # relies on each call site using a distinct phone_number for isolation
    # (see scripts/test_intent_router.py's per-case phone numbers), so
    # phone_number doubles as thread_id here, same 1:1 relationship this
    # function always assumed pre-Phase-4.
    config = session_identity(phone_number, thread_id=phone_number)
    checkpoint_tuple = await checkpointer.aget_tuple(config)

    history: list[AnyMessage] = []
    if checkpoint_tuple is not None:
        history = checkpoint_tuple.checkpoint.get("channel_values", {}).get("messages", [])
    bounded_history = history[-BOUNDED_HISTORY_LEN:]

    agent = build_router_agent()
    result = await agent.ainvoke(
        {"messages": [*bounded_history, HumanMessage(content=message)]}
    )
    return result["structured_response"]
