"""Session identity + the shared PostgresSaver checkpointer.

Session identity convention: phone_number == user_id == session_id, still
true and unaffected by Phase 4. `thread_id` is no longer derived from
phone_number, though — decoupled in Phase 4 so a client (not a
phone-number-only integration) can own conversation boundaries itself, the
way a normal chat UI's own "New Chat" button already does. `phone_number`
stays the permanent customer identity, keying `recipients`/`orders`/
`order_products`/`threads`; `thread_id` is per-conversation, generated
fresh by `src/pipeline.py::run_turn` when a caller doesn't supply one.
"""

import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from src.config import DATABASE_URL
from src.db.conninfo import ipv4_conninfo

# psycopg3's async mode needs WindowsSelectorEventLoopPolicy — the default
# ProactorEventLoop doesn't support it. Must be set before any event loop is
# created, so this runs at import time.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def session_identity(phone_number: str, thread_id: str) -> dict:
    """RunnableConfig for a turn. user_id/session_id stay tied to
    phone_number (customer identity); thread_id is an independent
    parameter now, resolved by the caller (src/pipeline.py::run_turn) —
    generated fresh when the client doesn't supply one, so one phone
    number can now have many independent conversations instead of exactly
    one, forever.
    """
    return {
        "configurable": {
            "thread_id": thread_id,
            "user_id": phone_number,
            "session_id": phone_number,
        }
    }


_checkpointer: AsyncPostgresSaver | None = None
_checkpointer_cm = None
_checkpointer_lock = asyncio.Lock()


async def get_checkpointer() -> AsyncPostgresSaver:
    """Cached singleton AsyncPostgresSaver, pointed at the shared Neon DB."""
    global _checkpointer, _checkpointer_cm
    if _checkpointer is not None:
        return _checkpointer
    async with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer
        cm = AsyncPostgresSaver.from_conn_string(ipv4_conninfo(DATABASE_URL))
        saver = await cm.__aenter__()
        await saver.setup()
        _checkpointer_cm = cm
        _checkpointer = saver
        return _checkpointer
