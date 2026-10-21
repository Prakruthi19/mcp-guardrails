"""
Shared agent-construction logic used by both agent/main.py (interactive
CLI) and eval/run_eval.py (batch eval harness).

LLM: GitHub Models' free-tier GPT-4o-mini, accessed through its
OpenAI-compatible chat completions endpoint via langchain_openai.ChatOpenAI,
authenticated with the GITHUB_TOKEN environment variable (a GitHub personal
access token -- no separate API key or paid account needed).

MCP: the storage-test-automation server (mcp_server/server.py) is launched
as a stdio subprocess via langchain_mcp_adapters.MultiServerMCPClient, and
its tools are handed to a LangGraph create_react_agent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GITHUB_MODELS_BASE_URL = os.environ.get(
    "GITHUB_MODELS_BASE_URL", "https://models.github.ai/inference"
)
GITHUB_MODELS_MODEL = os.environ.get("GITHUB_MODELS_MODEL", "openai/gpt-4o-mini")

SYSTEM_PROMPT = """\
You are a storage-device test-automation assistant for NVMe SSDs. You have
three tools, all backed by SIMULATED/mock data (no physical hardware):

- check_device_health(device_id): mock SMART-style telemetry, including a
  health_flag of "healthy", "warning", or "critical".
- run_device_test(device_id, test_suite): actually runs a simulated test
  and can return "pass" or "fail". test_suite is one of
  read_write_endurance, latency_benchmark, power_cycle.
- get_test_schedule(device_id, action): view or add to that device's
  upcoming test schedule.

Core workflow -- follow this whenever asked to check on, evaluate, or
decide about a device:
1. ALWAYS call check_device_health first before running or recommending
   a test. Do not run a test on a device you have not just checked.
2. Decide whether a test is warranted based on health_flag and
   percentage_used. A "healthy" device generally does not need an
   immediate test unless the user explicitly asks for one. A "warning"
   or "critical" device warrants running a relevant test_suite.
3. If you run a test and it fails, do not report it as routine. Read
   failure_reason and recommended_action from the result:
   - "escalate_rerun": recommend scheduling a re-test soon (you may use
     get_test_schedule with action="add" to do this) and flag it for
     human follow-up.
   - "escalate_replace": this is a critical-health device that failed --
     clearly recommend the device be flagged for replacement, not just
     a re-run.
4. Always state which tools you called and why in your final answer, and
   be explicit that all data is simulated/mock when relevant.

Be concise and concrete. Cite the actual numbers (wear %, temperature,
health_flag, test status) you observed rather than vague summaries.
"""


def get_llm() -> ChatOpenAI:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set. Create a GitHub personal access token "
            "(no special scopes needed for GitHub Models) and set it as "
            "GITHUB_TOKEN, e.g. in a .env file -- see .env.example."
        )
    return ChatOpenAI(
        model=GITHUB_MODELS_MODEL,
        base_url=GITHUB_MODELS_BASE_URL,
        api_key=token,
        temperature=0,
    )


def get_mcp_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "storage-test-automation": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "mcp_server.server"],
                "cwd": str(PROJECT_ROOT),
            }
        }
    )


async def build_agent():
    """Return (agent, tools). Caller keeps the client's tools alive for reuse."""
    client = get_mcp_client()
    tools = await client.get_tools()
    llm = get_llm()
    agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)
    return agent, tools


async def ask(agent, question: str) -> dict[str, Any]:
    """Invoke the agent on one question. Returns final text + full message list."""
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    messages: list[BaseMessage] = result["messages"]
    final_text = messages[-1].content if messages else ""
    return {"final_text": final_text, "messages": messages}


def extract_tool_call_names(messages: list[BaseMessage]) -> list[str]:
    """Return the ordered list of tool names the agent actually called."""
    names: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            names.extend(tc["name"] for tc in msg.tool_calls)
    return names


def format_trace(messages: list[BaseMessage]) -> str:
    """Render the tool-call sequence from a message list as a readable trace.

    This is the interview talking point: which tools were called, in what
    order, with what arguments, and what they returned.
    """
    lines: list[str] = []
    step = 0
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                step += 1
                lines.append(f"  [{step}] CALL {tc['name']}({tc['args']})")
        elif isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if len(content) > 300:
                content = content[:300] + "...(truncated)"
            lines.append(f"      -> {msg.name}: {content}")
    if not lines:
        return "  (no tool calls)"
    return "\n".join(lines)
