"""Phase 3.5 live end-to-end check, through the real orchestrator graph
(pipeline.run_turn) against real Kapruka MCP tools + Gemini — not just the
Checkout Router in isolation (scripts/test_checkout_router.py covers that).

Verifies the actual bug this phase fixes: a mid-collecting_delivery
digression must NOT be swallowed as a literal field value by
handle_collecting_delivery. Never sends a real "yes" at awaiting_confirm
(same deliberate boundary as every other live checkout test in this repo).

Run: .venv/Scripts/python.exe scripts/test_checkout_router_e2e.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.pipeline import run_turn
from src.session import get_checkpointer, session_identity

PHONE = "+94_test_35_e2e_3"


async def dump_state(label: str) -> dict:
    checkpointer = await get_checkpointer()
    tuple_ = await checkpointer.aget_tuple(session_identity(PHONE))
    values = tuple_.checkpoint.get("channel_values", {})
    print(f"\n--- state after {label} ---")
    print("stage:", values.get("stage"))
    print("collecting_field:", values.get("collecting_field"))
    print("checkout_info:", values.get("checkout_info"))
    print("cart items:", [i.get("name") for i in (values.get("cart") or {}).get("items", [])])
    return values


async def main() -> None:
    reply0 = await run_turn(
        PHONE,
        "I want to send a flower bouquet for my colleague Kasun's birthday, budget doesn't matter",
    )
    print("\n[assistant] turn 0 (search):", reply0)

    reply1 = await run_turn(
        PHONE,
        "The first one looks great, let's go with that. Deliver to Colombo 03 on 2026-10-15.",
    )
    print("\n[assistant] turn 1 (confirm+propose):", reply1)
    values = await dump_state("turn 1 (expect stage=collecting_delivery, asking a Gate-3 field)")
    stage1 = values.get("stage")
    field1 = values.get("collecting_field")
    assert stage1 == "collecting_delivery", f"expected collecting_delivery, got {stage1}"

    # Digression: does NOT answer the pending field (e.g. recipient_name).
    reply2 = await run_turn(PHONE, "wait, actually what payment methods do you accept?")
    print("\n[assistant] turn 2 (digression):", reply2)
    values2 = await dump_state("turn 2 (digression)")

    field_value_polluted = (
        values2.get("checkout_info", {}).get(field1) == "wait, actually what payment methods do you accept?"
    )
    print(f"\n[CHECK] field '{field1}' polluted with raw digression text: {field_value_polluted}")
    print("[EXPECT] False -- the Checkout Router should have caught this as modify_request/unrelated, "
          "not handle_collecting_delivery consuming it as the literal answer.")

    # Cancel outright.
    reply3 = await run_turn(PHONE, "actually never mind, please cancel this order")
    print("\n[assistant] turn 3 (cancel):", reply3)
    values3 = await dump_state("turn 3 (cancel)")
    cancelled_cleanly = (
        values3.get("stage") is None
        and not (values3.get("cart") or {}).get("items")
        and not values3.get("checkout_info")
    )
    print(f"\n[CHECK] checkout state fully cleared after cancel: {cancelled_cleanly}")


if __name__ == "__main__":
    asyncio.run(main())
