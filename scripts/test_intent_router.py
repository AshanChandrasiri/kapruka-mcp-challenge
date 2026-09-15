"""Phase 1 check: hand-written messages per intent, plus adversarial
out_of_scope/chitchat boundary cases (shared-vocabulary false positives).

Uses classify_intent() directly — the standalone, bounded-history path —
each case gets its own phone_number so they don't share session history.

Run: .venv/Scripts/python.exe scripts/test_intent_router.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.router.intent_router import classify_intent

# (message, expected_intent)
CASES: list[tuple[str, str]] = [
    # One straightforward case per intent.
    ("I want to send a flower bouquet and chocolates to my wife for our anniversary", "gift_request"),
    ("What's the status of my order KAP-99213?", "track_order"),
    ("The cake I received was crushed, I want a refund", "return_item"),
    ("Hi, good morning!", "chitchat"),
    ("What is the capital of France?", "out_of_scope"),
    # Adversarial / boundary cases — shared vocabulary that could be
    # dragged into the wrong category.
    ("can you check this product on eBay", "out_of_scope"),
    ("is eBay better than you", "out_of_scope"),
    ("what's the president of Sri Lanka", "out_of_scope"),
    ("what can you do?", "chitchat"),
    ("thanks, bye!", "chitchat"),
]


async def main() -> None:
    passed = 0
    for i, (message, expected) in enumerate(CASES):
        result = await classify_intent(f"+94_test_case_{i}", message)
        ok = result.intent == expected
        passed += ok
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {message!r}")
        print(f"       expected={expected} got={result.intent}")

    print(f"\n{passed}/{len(CASES)} passed")


if __name__ == "__main__":
    asyncio.run(main())
