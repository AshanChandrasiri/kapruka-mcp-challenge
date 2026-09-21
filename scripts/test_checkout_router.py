"""Phase 3.5 check: hand-written (stage, collecting_field, cart_summary,
message) cases for the Checkout Router, covering each of its four intents
plus the answers_pending extraction it's supposed to do in the same call.

Uses classify_checkout_intent() directly — the standalone path, mirroring
scripts/test_intent_router.py's use of classify_intent().

Run: .venv/Scripts/python.exe scripts/test_checkout_router.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.router.checkout_router import classify_checkout_intent

CART_SUMMARY = (
    "items=[Red Rose Bouquet, Belgian Chocolate Box], estimated_total=6500.0, "
    "collected_so_far={}"
)
CONFIRM_CART_SUMMARY = (
    "items=[Red Rose Bouquet, Belgian Chocolate Box], estimated_total=6500.0, "
    "collected_so_far={'delivery_city': 'Colombo 05', 'delivery_date': '2026-09-25', "
    "'recipient_name': 'Nadeesha', 'recipient_phone': '0771234567', "
    "'delivery_address': '12 Flower Rd', 'sender_name': 'Ashan'}"
)

# (stage, collecting_field, cart_summary, message, expected_intent, expected_value_or_None)
CASES: list[tuple[str, str | None, str, str, str, str | None]] = [
    # answers_pending — plausible answers to the pending field, with extraction.
    ("collecting_delivery", "delivery_city", CART_SUMMARY,
     "yeah ship it to Colombo 05", "answers_pending", "Colombo 05"),
    ("collecting_delivery", "delivery_date", CART_SUMMARY,
     "let's do the 25th of September 2026, so 2026-09-25", "answers_pending", "2026-09-25"),
    ("collecting_delivery", "recipient_phone", CART_SUMMARY,
     "0771234567", "answers_pending", "0771234567"),
    # modify_request — wants to change the order instead of answering.
    ("collecting_delivery", "delivery_city", CART_SUMMARY,
     "actually I want to change the order, swap the chocolates for something else",
     "modify_request", None),
    # cancel_checkout — wants to stop entirely.
    ("collecting_delivery", "delivery_date", CART_SUMMARY,
     "never mind, cancel this", "cancel_checkout", None),
    ("awaiting_confirm", None, CONFIRM_CART_SUMMARY,
     "actually cancel this please", "cancel_checkout", None),
    # unrelated — a genuine tangent mid-checkout.
    ("collecting_delivery", "delivery_city", CART_SUMMARY,
     "what's the weather like today", "unrelated", None),
    # awaiting_confirm — a genuine yes still classifies as answers_pending
    # (note: the classifier's opinion is NOT the confirmation gate — that's
    # _is_confirmation, checked independently on the raw text elsewhere).
    ("awaiting_confirm", None, CONFIRM_CART_SUMMARY,
     "yes", "answers_pending", "yes"),
    ("awaiting_confirm", None, CONFIRM_CART_SUMMARY,
     "no, hold on, I want to add a card too", "modify_request", None),
]


async def main() -> None:
    passed = 0
    for i, (stage, field, cart_summary, message, expected_intent, expected_value) in enumerate(CASES):
        result = await classify_checkout_intent(
            f"+94_test_case_{i}", message, stage, field, cart_summary
        )
        intent_ok = result.intent == expected_intent
        value_ok = True
        if expected_intent == "answers_pending":
            # Loose check on extracted_value — exact wording can vary
            # slightly; require it's non-empty and matches when we have a
            # precise expectation (city/date/phone are unambiguous).
            value_ok = bool(result.extracted_value) and (
                expected_value is None or expected_value.lower() in (result.extracted_value or "").lower()
            )
        ok = intent_ok and value_ok
        passed += ok
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] stage={stage} field={field} msg={message!r}")
        print(f"       expected=({expected_intent}, {expected_value!r}) got=({result.intent}, {result.extracted_value!r})")

    print(f"\n{passed}/{len(CASES)} passed")


if __name__ == "__main__":
    asyncio.run(main())
