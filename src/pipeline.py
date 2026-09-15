"""Shared "run one turn" call. Every entry point (console, FastAPI webhook,
Gradio dev UI) drives the graph through this one function.
"""

import asyncio

from langchain_core.messages import HumanMessage

from src.orchestrator import build_orchestrator
from src.session import session_identity

_orchestrator = None
_orchestrator_lock = asyncio.Lock()


async def _get_orchestrator():
    global _orchestrator
    if _orchestrator is not None:
        return _orchestrator
    async with _orchestrator_lock:
        if _orchestrator is not None:
            return _orchestrator
        _orchestrator = await build_orchestrator()
        return _orchestrator


async def run_turn(phone_number: str, message: str) -> str:
    """Run one inbound message through the compiled graph, return the reply text."""
    orchestrator = await _get_orchestrator()
    config = session_identity(phone_number)
    result = await orchestrator.ainvoke(
        {"messages": [HumanMessage(content=message)]}, config=config
    )
    return result["messages"][-1].text
