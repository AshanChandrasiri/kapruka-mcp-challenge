"""Session identity + the shared PostgresSaver checkpointer.

Session identity convention, used by every component: phone_number ==
user_id == session_id — one shared conversation per phone number, not a
per-component one.
"""

import asyncio
import socket
import sys
from urllib.parse import urlparse

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo

from src.config import DATABASE_URL

# psycopg3's async mode needs WindowsSelectorEventLoopPolicy — the default
# ProactorEventLoop doesn't support it. Must be set before any event loop is
# created, so this runs at import time.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def session_identity(phone_number: str) -> dict:
    """RunnableConfig for a turn, keyed by phone_number == user_id == session_id."""
    return {
        "configurable": {
            "thread_id": phone_number,
            "user_id": phone_number,
            "session_id": phone_number,
        }
    }


def _ipv4_conninfo(database_url: str) -> str:
    """Force IPv4 for the Neon host.

    This environment's DNS returns AAAA records for the Neon pooler, but has
    no working outbound IPv6 route — a plain connect on the hostname hangs
    (TCP SYN silently dropped) instead of failing fast. Resolving the A
    record ourselves and passing it as `hostaddr` skips libpq's own DNS
    resolution while `host` is kept for the TLS/SCRAM channel binding Neon
    requires. See scripts/check_db.py for the sync-path equivalent.
    """
    host = urlparse(database_url).hostname
    ipv4 = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    return make_conninfo(database_url, hostaddr=ipv4)


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
        cm = AsyncPostgresSaver.from_conn_string(_ipv4_conninfo(DATABASE_URL))
        saver = await cm.__aenter__()
        await saver.setup()
        _checkpointer_cm = cm
        _checkpointer = saver
        return _checkpointer
