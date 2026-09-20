"""Check Delivery — once per distinct product, not once per cart.
Deliverability is scoped per item (food/liquor/hotel-cake items reach far
fewer cities than flowers); an order ships as one shipment, so the whole
cart must be deliverable to the city.
"""

from dataclasses import dataclass, field
from typing import Literal

from src.checkout.mcp_client import call_kapruka_tool
from src.gift_picker.state import Cart


@dataclass
class CityResolution:
    """Result of resolving a customer's free-text city answer against
    kapruka_list_delivery_cities, BEFORE any kapruka_check_delivery call —
    a cheap validation pass, not a deliverability check.

    - "exact": name matched (case-insensitive) — use `canonical` as-is.
    - "alias": matched one of the city's aliases, not its canonical name —
      the customer's spelling is ambiguous enough to confirm before
      accepting (e.g. "chilaw" -> "Chillaw").
    - "suggestions": no exact/alias hit, but the search returned partial
      matches worth offering back.
    - "no_match": nothing in the delivery network resembles this at all.
    """

    status: Literal["exact", "alias", "suggestions", "no_match"]
    canonical: str | None = None
    candidates: list[str] = field(default_factory=list)


async def resolve_city(query: str) -> CityResolution:
    """`kapruka_list_delivery_cities` only ever returns up to 50 of the
    network's 332 cities (confirmed live) — it's a search endpoint, not a
    directory dump, so this validates the customer's own answer against it
    rather than trying to present the full list.

    Note on the live data: a city's `aliases` sometimes come back as one
    space-joined blob of several alias words in a single string, not a
    clean one-alias-per-entry list (confirmed live, e.g. Anuradhapura's
    aliases field is `["anuradapura galenbindunuwewa anuradhapue"]`) — so
    alias matching splits each alias string on whitespace and compares
    tokens, not whole strings.
    """
    result = await call_kapruka_tool(
        "kapruka_list_delivery_cities", {"query": query, "limit": 8}
    )
    cities = result.get("cities", [])
    query_norm = query.strip().lower()

    for city in cities:
        if city["name"].strip().lower() == query_norm:
            return CityResolution(status="exact", canonical=city["name"])

    for city in cities:
        alias_tokens = {
            token.lower()
            for alias in city.get("aliases", [])
            for token in alias.split()
        }
        if query_norm in alias_tokens:
            return CityResolution(status="alias", canonical=city["name"])

    if not cities:
        return CityResolution(status="no_match")

    return CityResolution(status="suggestions", candidates=[c["name"] for c in cities])


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
