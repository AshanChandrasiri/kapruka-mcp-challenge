"""Shared "run one turn" call. Every entry point (console, FastAPI webhook,
Gradio dev UI) drives the graph through this one function.
"""

import asyncio
import uuid

from langchain_core.messages import HumanMessage
from opentelemetry.trace import SpanKind

from src import observability
from src.checkout.compaction import compact_thread
from src.db.threads import touch_thread
from src.orchestrator import build_orchestrator
from src.session import get_checkpointer, session_identity

_orchestrator = None
_orchestrator_lock = asyncio.Lock()


async def _get_orchestrator():
    global _orchestrator
    if _orchestrator is not None:
        return _orchestrator
    async with _orchestrator_lock:
        if _orchestrator is not None:
            return _orchestrator
        _orchestrator = await build_orchestrator()
        return _orchestrator


async def run_turn(phone_number: str, message: str, thread_id: str | None = None) -> tuple[str, str]:
    """Run one inbound message through the compiled graph, return
    (reply_text, thread_id). Phase 4: thread_id is the caller's own
    conversation-boundary mechanism (a "New Chat" button, in a normal chat
    UI) — absent means a new conversation, so one is generated here and
    handed back for the caller to persist and pass on the next call.
    phone_number stays the permanent customer identity regardless.
    """
    resolved_thread_id = thread_id or str(uuid.uuid4())
    await touch_thread(resolved_thread_id, phone_number)

    orchestrator = await _get_orchestrator()
    config = session_identity(phone_number, resolved_thread_id)

    # Phase 5.0.1: SpanKind.SERVER specifically matters here, not just as a
    # formality — a console app emits no server-shaped spans otherwise,
    # since nothing else in the process would create one. turn_context
    # must wrap the span (not the other way around) so the baggage it
    # attaches is already the "current" context by the time this span and
    # everything nested under it (the Intent Router's own LLM call, once
    # instrumented) gets created.
    with observability.turn_context(phone_number, resolved_thread_id):
        tracer = observability.get_tracer()
        with tracer.start_as_current_span("run_turn", kind=SpanKind.SERVER) as span:
            result = await orchestrator.ainvoke(
                {"messages": [HumanMessage(content=message)]}, config=config
            )
            # Domain-specific enrichment the auto-instrumentation won't
            # produce on its own — absent whenever this turn never reached
            # the Intent Router (already mid-checkout, stage was set).
            intent = result.get("intent")
            if intent:
                span.set_attribute("gift.intent", intent)

    compaction_summary = result.get("order_summary_for_compaction")
    if compaction_summary:
        checkpointer = await get_checkpointer()
        await compact_thread(orchestrator, checkpointer, config, compaction_summary)

    return result["messages"][-1].text, resolved_thread_id
