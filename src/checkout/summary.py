"""Show Summary — items, prices, delivery fee, total, perishable notes,
delivery/recipient/sender details. Plain text, no LLM involved.
"""

from src.gift_picker.state import Cart


def _item_price(item: dict) -> float:
    """The Gift-Picker is told to always flatten price to a plain number, but
    an LLM's compliance with a docstring isn't a guarantee — defensively
    handle the {"amount": ..., "currency": ...} shape too if it slips
    through.
    """
    price = item.get("price", 0)
    if isinstance(price, dict):
        return price.get("amount", 0)
    return price


def build_summary(cart: Cart, checkout_info: dict) -> str:
    lines = ["Here's your order summary:", ""]

    items_total = 0.0
    for item in cart["items"]:
        price = _item_price(item)
        items_total += price
        lines.append(f"- {item.get('name', item['product_id'])} — {item.get('currency', 'LKR')} {price:,.0f}")

    delivery_fee = checkout_info.get("delivery_fee", 0)
    grand_total = items_total + delivery_fee

    lines.append("")
    lines.append(f"Items total: LKR {items_total:,.0f}")
    lines.append(f"Delivery fee: LKR {delivery_fee:,.0f}")
    lines.append(f"Total: LKR {grand_total:,.0f}")

    for warning in checkout_info.get("perishable_warnings", []):
        lines.append("")
        # Kapruka's own perishable_warning text already reads like "Note:
        # ..." — don't double up on the prefix.
        lines.append(warning)

    lines.append("")
    lines.append(f"Deliver to: {checkout_info['recipient_name']} ({checkout_info['recipient_phone']})")
    lines.append(f"Address: {checkout_info['delivery_address']}, {checkout_info['delivery_city']}")
    lines.append(f"Date: {checkout_info['delivery_date']}")
    lines.append(f"From: {checkout_info['sender_name']}")
    lines.append("")
    lines.append("Reply YES to confirm and get your payment link, or let me know what to change.")

    return "\n".join(lines)
