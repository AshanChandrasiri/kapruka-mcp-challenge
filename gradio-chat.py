"""Gradio dev/demo UI. Same shared "run one turn" call as main.py
(src/pipeline.py::run_turn()) and the future FastAPI webhook — this file
only swaps main.py's while-loop for a gr.ChatInterface, nothing else about
how a turn is run changes.

Deliberately does NOT use Gradio's own chat-history state as memory —
conversation memory is the Postgres session (src/session.py), same as every
other entry point. Each browser session gets one throwaway phone_number
identity instead, generated once via gr.State's callable-default (called
per app load, i.e. per session) and threaded through every respond() call
as the session's session_identity key.

Windows-only wrinkle, confirmed by testing: Gradio's own server ends up
running request handlers on a ProactorEventLoop regardless of the
WindowsSelectorEventLoopPolicy set in src/session.py — that fix alone isn't
enough once Gradio/uvicorn's own loop is already running, and psycopg3's
async mode needs a selector loop. So respond() below doesn't just
asyncio.run() the pipeline call on whatever loop handled the HTTP request;
it hands the call to a short-lived thread and builds a fresh event loop
there instead. A brand-new thread has no event loop of its own yet, so
asyncio.new_event_loop() there picks up the process-wide policy
src/session.py already set (import-time, before Gradio ever starts) rather
than whatever Gradio's request handler happened to be running on.
main.py doesn't need any of this — a bare asyncio.run() in the main thread
picks up the policy fine on its own.
"""

import asyncio
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import gradio as gr

from src.pipeline import run_turn

sys.stdout.reconfigure(encoding="utf-8")


def _new_phone_number() -> str:
    return f"+94_gradio_{uuid.uuid4().hex[:8]}"


def _run_turn_in_fresh_loop(phone_number: str, message: str) -> str:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run_turn(phone_number, message))
    finally:
        loop.close()


def respond(message: str, history: list, phone_number: str) -> str:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_run_turn_in_fresh_loop, phone_number, message).result()


demo = gr.ChatInterface(
    fn=respond,
    additional_inputs=[gr.State(_new_phone_number)],
    title="Kapruka Gift Concierge",
    description="Dev/demo UI — one throwaway session per browser tab, backed by the same Postgres-checkpointed graph as the console app.",
)

if __name__ == "__main__":
    demo.launch()
