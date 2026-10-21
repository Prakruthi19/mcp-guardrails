"""
Eval harness: runs the agent against eval/test_questions.json and prints
the full tool-call trace for each question (tool names, args, order) --
this trace is the interview talking point for the "agentic workflow"
claim, so it is always printed, not just a pass/fail summary.

Usage:
    python -m eval.run_eval
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.agent_client import ask, build_agent, extract_tool_call_names, format_trace

QUESTIONS_FILE = Path(__file__).resolve().parent / "test_questions.json"


def check_expectation(case: dict, called_tools: list[str]) -> tuple[bool, str]:
    expected = set(case["expected_tools"])
    called = set(called_tools)
    missing = expected - called
    if missing:
        return False, f"missing expected tool call(s): {sorted(missing)}"

    if case.get("requires_chain"):
        if "check_device_health" not in called_tools or "run_device_test" not in called_tools:
            return False, "chain case did not call both check_device_health and run_device_test"
        if called_tools.index("check_device_health") > called_tools.index("run_device_test"):
            return False, "run_device_test was called before check_device_health"

    return True, "ok"


async def main() -> None:
    cases = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(cases)} eval cases from {QUESTIONS_FILE.name}\n")

    print("Starting MCP server and connecting agent...")
    agent, _tools = await build_agent()
    print("Ready.\n")
    print("=" * 78)

    results = []
    for case in cases:
        print(f"[{case['id']}] {case['question']}")
        result = await ask(agent, case["question"])
        called_tools = extract_tool_call_names(result["messages"])

        print("tool trace:")
        print(format_trace(result["messages"]))
        print("final answer:")
        print(f"  {result['final_text']}")

        passed, reason = check_expectation(case, called_tools)
        status = "PASS" if passed else "FAIL"
        print(f"eval check: {status} ({reason})")
        print("=" * 78)

        results.append({"id": case["id"], "status": status, "reason": reason, "called_tools": called_tools})

    n_pass = sum(1 for r in results if r["status"] == "PASS")
    print(f"\nSummary: {n_pass}/{len(results)} passed\n")
    for r in results:
        print(f"  {r['status']:4s}  {r['id']:35s} tools={r['called_tools']}")


if __name__ == "__main__":
    asyncio.run(main())
