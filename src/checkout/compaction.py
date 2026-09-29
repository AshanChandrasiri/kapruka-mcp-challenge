"""Phase 4: post-order history compaction. Once a New Chat boundary
(src/pipeline.py::run_turn's thread_id generation) handles the common case
of unbounded history growth, this becomes a narrower safety net — a
customer who keeps talking in the SAME thread after checkout completes.

Deliberately its own step, called from src/pipeline.py::run_turn only
after the graph invocation that ran complete_order has already returned
(so its own checkpoint write has already landed) — never from inside a
still-running graph invocation, where this module's own checkpoint writes
would race the graph's own end-of-step checkpoint write and lose. Also
deliberately decoupled from complete_order's own transaction
(src/checkout/flow.py): a compaction failure must never touch the one
irreversible action in this codebase, which has already succeeded by the
time this runs.

Not atomic: adelete_thread then aupdate_state are two separate calls — a
crash between them leaves the thread's checkpoint history empty, not
corrupted. Accepted on purpose: the order itself is already safely
committed to orders/order_products by this point, so the worst case is
lost chat context, not lost order data. Logged, not swallowed, if it fails
— compaction is a cleanup step, not something a customer's turn should
ever fail on.
"""

import logging

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

logger = logging.getLogger(__name__)


async def compact_thread(
    graph,
    checkpointer: AsyncPostgresSaver,
    config: RunnableConfig,
    summary_text: str,
) -> None:
    """Replace (not append to) a thread's checkpointed history with a short
    summary. `as_node="confirm_agent"` — the node whose turn just produced
    the completed order this summary describes; LangGraph only needs a
    real node name to attribute the write to, not one with any special
    meaning here since there's no prior checkpoint left to reconcile
    against after adelete_thread.
    """
    thread_id = config["configurable"]["thread_id"]
    try:
        await checkpointer.adelete_thread(thread_id)
        await graph.aupdate_state(
            config,
            {
                "messages": [AIMessage(content=summary_text)],
                "order_summary_for_compaction": None,
            },
            as_node="confirm_agent",
        )
    except Exception:
        logger.exception("Post-order history compaction failed for thread %s", thread_id)
