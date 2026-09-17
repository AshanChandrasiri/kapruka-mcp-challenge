"""Check Delivery — once per distinct product, not once per cart.
Deliverability is scoped per item (food/liquor/hotel-cake items reach far
fewer cities than flowers); an order ships as one shipment, so the whole
cart must be deliverable to the city.
"""

from dataclasses import dataclass, field

from src.checkout.mcp_client import call_kapruka_tool
from src.gift_picker.state import Cart


@dataclass
class DeliveryCheckResult:
    ok: bool
    fee: float | None = None
    perishable_warnings: list[str] = field(default_factory=list)
    failed_items: list[dict] = field(default_factory=list)  # [{product_id, name, reason}]


async def check_delivery_for_cart(cart: Cart, city: str, delivery_date: str) -> DeliveryCheckResult:
    items_by_product_id = {item["product_id"]: item for item in cart["items"]}

    fees: list[float] = []
    warnings: list[str] = []
    failed: list[dict] = []

    for product_id, item in items_by_product_id.items():
        result = await call_kapruka_tool(
            "kapruka_check_delivery",
            {"city": city, "delivery_date": delivery_date, "product_id": product_id},
        )
        if not result.get("available", False):
            failed.append(
                {
                    "product_id": product_id,
                    "name": item.get("name", product_id),
                    "reason": result.get("reason") or "not deliverable to this city/date",
                }
            )
            continue
        fees.append(result.get("rate", 0))
        if result.get("perishable_warning"):
            warnings.append(result["perishable_warning"])

    if failed:
        return DeliveryCheckResult(ok=False, failed_items=failed)

    # Flat rate per order — Kapruka's own docs call it "flat", but taking the
    # max across items is a cheap defensive hedge against any per-call
    # inconsistency rather than trusting they're always identical.
    return DeliveryCheckResult(ok=True, fee=max(fees) if fees else 0, perishable_warnings=warnings)
