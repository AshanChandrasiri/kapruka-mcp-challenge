"""Phase 0 check: Gemini API key works, and a LangGraph agent node can make
a basic tool-calling request.

Run: .venv/Scripts/python.exe scripts/check_llm.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

from src.config import GOOGLE_API_KEY, LLM_MODEL


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def main() -> None:
    print(f"Using model: {LLM_MODEL}")
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, google_api_key=GOOGLE_API_KEY)
    agent = create_agent(llm, tools=[add])

    result = await agent.ainvoke(
        {"messages": [("user", "What is 47 plus 55? Use the add tool.")]}
    )
    final = result["messages"][-1]
    print("Final message:", final.content)

    called_tool = any(
        getattr(m, "tool_calls", None) for m in result["messages"]
    )
    print("Tool call observed:", called_tool)


if __name__ == "__main__":
    asyncio.run(main())
