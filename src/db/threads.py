"""Thread <-> phone_number mapping (Phase 4). Raw async psycopg (a
connection pool), same as the rest of src/db — no ORM.

AsyncPostgresSaver's own checkpoint tables only know about thread_id;
without this table there's no way to answer "which customer does this
thread belong to" at all.
"""

import asyncio

from psycopg_pool import AsyncConnectionPool

from src import observability
from src.config import DATABASE_URL
from src.db.conninfo import ipv4_conninfo

_pool: AsyncConnectionPool | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        pool = AsyncConnectionPool(ipv4_conninfo(DATABASE_URL), min_size=1, max_size=5, open=False)
        await pool.open()
        _pool = pool
        return _pool


async def touch_thread(thread_id: str, phone_number: str) -> None:
    """Record activity on a thread — inserts a new row the first time a
    thread_id is seen, updates last_active_at every time (including that
    same first call), via a single upsert rather than a separate
    exists-check: a caller passing back a thread_id we already issued (the
    common case) or a never-before-seen one (a freshly generated one, or a
    client-supplied ID we've never seen) both land correctly in one
    statement.

    Phase 5.0.3: a lighter span, mainly for timing this DB write rather
    than for business attributes — thread_id/phone_number are already
    covered as baggage on spans created within a turn_context anyway.
    """
    tracer = observability.get_tracer()
    with tracer.start_as_current_span("db.touch_thread"):
        pool = await _get_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO threads (thread_id, phone_number) VALUES (%s, %s) "
                    "ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()",
                    (thread_id, phone_number),
                )
