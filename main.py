"""
Interactive CLI demo loop for the storage-test-automation-mcp agent.

Starts the MCP server as a stdio subprocess, wires it into a LangGraph
ReAct agent backed by GitHub Models GPT-4o-mini, and lets you ask
free-form questions about mock NVMe devices (nvme0n1, nvme1n1, nvme2n1,
nvme3n1). Every device-health and test-result value is simulated -- see
README.md.

Usage:
    python main.py
    python main.py "Check nvme2n1 and run a test if it needs one"
"""

from __future__ import annotations

import asyncio
import sys

from agent.agent_client import ask, build_agent, format_trace

BANNER = """\
Storage Device Test Automation Agent (MCP + LangGraph, mock data demo)
Known devices: nvme0n1 (healthy), nvme1n1 (warning), nvme2n1 (critical), nvme3n1 (fresh)
Type a question, or 'exit' to quit.
"""


async def run_one(agent, question: str) -> None:
    result = await ask(agent, question)
    print("\n--- tool trace ---")
    print(format_trace(result["messages"]))
    print("--- answer ---")
    print(result["final_text"])
    print()


async def main() -> None:
    print("Starting MCP server and connecting agent...")
    agent, _tools = await build_agent()
    print("Ready.\n")

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(f"> {question}")
        await run_one(agent, question)
        return

    print(BANNER)
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break
        await run_one(agent, question)


if __name__ == "__main__":
    asyncio.run(main())
