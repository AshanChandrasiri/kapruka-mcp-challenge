"""Recipient profile lookups for the Gift-Picker agent's `get_recipient_profile`
tool. Raw async psycopg (a connection pool), same as the rest of src/db —
no ORM.
"""

import asyncio

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.config import DATABASE_URL
from src.db.conninfo import ipv4_conninfo

_pool: AsyncConnectionPool | None = None
_pool_lock = asyncio.Lock()

_SELECT_COLUMNS = "name, relationship, preferences, budget_min, budget_max"


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


async def find_recipients(phone_number: str, query: str) -> list[dict]:
    """Loosely match `query` against a customer's saved recipients by name OR
    relationship. Falls back to the customer's full recipient list when the
    filtered match comes back empty — `query` may be a loose alias (e.g.
    "mom") that a substring filter won't catch against a stored value like
    relationship="mother"; the model is better placed to make that call than
    a hardcoded synonym table.
    """
    pool = await _get_pool()
    pattern = f"%{query}%"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_SELECT_COLUMNS} FROM recipients "
                "WHERE phone_number = %s AND (name ILIKE %s OR relationship ILIKE %s) "
                "ORDER BY created_at",
                (phone_number, pattern, pattern),
            )
            rows = await cur.fetchall()
            if rows:
                return rows

            await cur.execute(
                f"SELECT {_SELECT_COLUMNS} FROM recipients "
                "WHERE phone_number = %s ORDER BY created_at",
                (phone_number,),
            )
            return await cur.fetchall()
