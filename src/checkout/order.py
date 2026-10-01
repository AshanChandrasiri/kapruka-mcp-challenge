"""Checkout — kapruka_create_order has exactly one call site in this whole
codebase: create_order(), called only from
src/checkout/flow.py::complete_order, itself only ever invoked by
src/orchestrator.py's deterministic gate after the confirmation keyword
check (flow.py::is_confirmation) passes. Never call this from anywhere else.

Field names below (checkout_url, order_ref, summary.grand_total) are
confirmed directly against the live tool schema (see PLAN.md Phase 3) —
not guessed, unlike the v1 build's own note that these were inferred.
"""

import asyncio

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src import observability
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
        "cart": [
            {"product_id": item["product_id"], "quantity": item.get("quantity", 1)}
            for item in cart["items"]
        ],
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
    """Two-table insert (orders + one order_products row per cart item), run
    inside a single transaction — pool.connection()'s own context manager
    commits on clean exit / rolls back on exception, so a failure partway
    through never leaves an orders row with no matching order_products rows.

    summary.delivery_fee/addons_total/currency are read defensively
    (.get, allow null) — only summary.grand_total has actually been
    confirmed against a live response so far (Phase 3); the other fields
    are trusted the same cautious way _item_price already handles
    inconsistent price shapes elsewhere in this codebase.

    Phase 5.0.3: wrapped in its own span — this is the single spot in the
    whole system where a wrong number gets permanently written, genuinely
    worth having on record, distinct from the MCP-call span that already
    wraps kapruka_create_order itself one level up (src/checkout/flow.py's
    complete_order calls create_order, then this).
    """
    pool = await _get_pool()
    summary = order_result.get("summary", {})
    total_amount = summary.get("grand_total", cart["estimated_total"])
    tracer = observability.get_tracer()
    with tracer.start_as_current_span("db.save_order") as span:
        span.set_attribute("order.total_amount", float(total_amount))
        span.set_attribute("order.currency", summary.get("currency", "LKR"))
        span.set_attribute("order.delivery_city", checkout_info["delivery_city"])

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "INSERT INTO orders (phone_number, total_amount, items_total, delivery_fee, "
                    "addons_total, currency, delivery_city, delivery_date, delivery_address, "
                    "recipient_name, recipient_phone, sender_name, payment_url, "
                    "payment_url_expires_at, kapruka_order_ref, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "now() + interval '60 minutes', %s, %s) "
                    "RETURNING id",
                    (
                        phone_number,
                        total_amount,
                        summary.get("items_total"),
                        summary.get("delivery_fee"),
                        summary.get("addons_total"),
                        summary.get("currency", "LKR"),
                        checkout_info["delivery_city"],
                        checkout_info["delivery_date"],
                        checkout_info["delivery_address"],
                        checkout_info["recipient_name"],
                        checkout_info["recipient_phone"],
                        checkout_info["sender_name"],
                        order_result.get("checkout_url"),
                        order_result.get("order_ref"),
                        "pending_payment",
                    ),
                )
                order_id = (await cur.fetchone())["id"]

                for item in cart["items"]:
                    await cur.execute(
                        "INSERT INTO order_products (order_id, kapruka_product_id, product_name, "
                        "product_url, product_image_url, unit_price, quantity) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            order_id,
                            item["product_id"],
                            item.get("name", item["product_id"]),
                            item.get("url"),
                            item.get("image_url"),
                            item.get("price"),
                            item.get("quantity", 1),
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
