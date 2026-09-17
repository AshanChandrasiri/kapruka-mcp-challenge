"""Checkout — kapruka_create_order has exactly one call site in this whole
codebase: create_order(), called only from
src/checkout/flow.py::handle_awaiting_confirm after the deterministic
confirmation keyword check passes. Never call this from anywhere else.

Field names below (checkout_url, order_ref, summary.grand_total) are
confirmed directly against the live tool schema (see PLAN.md Phase 3) —
not guessed, unlike the v1 build's own note that these were inferred.
"""

import asyncio
import json

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.checkout.mcp_client import call_kapruka_tool
from src.config import DATABASE_URL
from src.db.conninfo import ipv4_conninfo
from src.gift_picker.state import Cart

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


async def create_order(cart: Cart, checkout_info: dict) -> dict:
    """Real, live financial action — a real 60-minute pay link. Only ever
    called after an explicit human confirmation (see flow.py).
    """
    currency = cart["items"][0].get("currency", "LKR") if cart["items"] else "LKR"
    params = {
        "cart": [{"product_id": item["product_id"], "quantity": 1} for item in cart["items"]],
        "recipient": {
            "name": checkout_info["recipient_name"],
            "phone": checkout_info["recipient_phone"],
        },
        "delivery": {
            "address": checkout_info["delivery_address"],
            "city": checkout_info["delivery_city"],
            "date": checkout_info["delivery_date"],
        },
        "sender": {"name": checkout_info["sender_name"]},
        "currency": currency,
    }
    return await call_kapruka_tool("kapruka_create_order", params)


async def save_order(phone_number: str, cart: Cart, checkout_info: dict, order_result: dict) -> None:
    pool = await _get_pool()
    product_summary = ", ".join(item.get("name", item["product_id"]) for item in cart["items"])
    total_amount = order_result.get("summary", {}).get("grand_total", cart["estimated_total"])
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO orders (phone_number, items, product_summary, total_amount, "
                "delivery_city, delivery_date, kapruka_order_id, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    phone_number,
                    json.dumps(cart["items"]),
                    product_summary,
                    total_amount,
                    checkout_info["delivery_city"],
                    checkout_info["delivery_date"],
                    order_result.get("order_ref"),
                    "pending_payment",
                ),
            )


async def track_order_once(order_ref: str) -> dict | None:
    """Best-effort immediate status check right after checkout. An "order
    not found" result here is expected (payment likely isn't complete yet
    since the customer hasn't opened the pay link), not an error.
    """
    try:
        return await call_kapruka_tool("kapruka_track_order", {"order_number": order_ref})
    except Exception:
        return None
