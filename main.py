"""Console entry point. One compiled graph per process, one turn per line
of input, driven through src/pipeline.py::run_turn() — the same call the
FastAPI webhook and Gradio dev UI will use later.
"""

import asyncio
import sys

from src.pipeline import run_turn

# Windows console defaults to cp1252, which can't encode the emoji in some
# canned responses.
sys.stdout.reconfigure(encoding="utf-8")

PHONE_NUMBER = "+94_console_dev"


async def main() -> None:
    print("Kapruka Gift Concierge (console) — Ctrl+C to quit\n")
    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue
        reply = await run_turn(PHONE_NUMBER, message)
        print(f"bot> {reply}\n")


if __name__ == "__main__":
    asyncio.run(main())
