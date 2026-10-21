# Storage Device Test Automation MCP Agent

An MVP demonstrating an MCP-based AI agent that automates storage device
(NVMe SSD) test execution and scheduling. It was scoped and built quickly
as a portfolio piece to show hands-on MCP protocol + agentic
test-automation workflow experience — it's a demonstration of the
pattern, not a production tool.

## Important: this is simulated data, not real hardware

**No physical NVMe device is touched anywhere in this project.** Device
health telemetry comes from a hand-authored mock JSON file
(`data/device_health.json`) whose field names mirror the real NVMe SMART
/ Health Information Log Page shape (`percentage_used`,
`composite_temperature_celsius`, `critical_warning`,
`reallocated_sector_count`, etc.) so the *shape* of the data is realistic
— the values themselves are made up. Test execution
(`run_device_test`) is a short simulated delay followed by a pass/fail
decision derived from that mock health data; it does not run any real
I/O against any real device. Treat every number this project produces as
synthetic.

## Why this exists

Test automation for storage devices is typically either (a) hardcoded
scripts triggering fixed test suites, or (b) manual triage of failures.
There's no standard way for an AI agent to reason about *which* test to
run, *when*, and *how* to interpret device health signals to decide next
steps. This project exposes device-test operations as MCP tools an LLM
agent can call, chain, and reason over — closer to how a test-automation
engineer actually thinks: check health → decide what to test →
run/schedule → triage failure.

This is grounded in real experience maintaining AWS-based Slurm HPC
clusters and lab-adjacent infrastructure at DOT Consulting, where
job/test scheduling and health monitoring of compute resources is
routine work. This project extends that experience into an agentic
pattern using MCP, applied to storage-device test automation as a
natural adjacent domain. It is not built around or dependent on any
other in-progress project.

## Architecture

```
User/CLI query -> LangGraph ReAct agent -> MCP client (stdio) -> MCP server -> tools -> mock JSON data
                          |
                    GitHub Models GPT-4o-mini (free tier)
```

Two processes: the MCP server (tool host, stdio transport,
`mcp_server/server.py`) and the agent client (LangGraph
`create_react_agent` + `langchain-mcp-adapters` `MultiServerMCPClient`,
`agent/agent_client.py`). The agent client launches the server as a
subprocess over stdio — this is the same pattern used for a real local
MCP deployment integrating an agent with lab/testing tools.

## MCP tools

| Tool | Purpose |
|---|---|
| `check_device_health(device_id)` | Mock SMART-style health telemetry (temperature, wear/`percentage_used`, reallocated sectors, power-on hours, critical_warning) plus a derived `health_flag` (`healthy` / `warning` / `critical`). |
| `run_device_test(device_id, test_suite)` | Simulates running `read_write_endurance`, `latency_benchmark`, or `power_cycle`. Failure probability is derived from the device's mock health record, so worn/critical devices fail more often — this is what makes the health → decision → test chain causally meaningful rather than cosmetic. |
| `get_test_schedule(device_id, action)` | View (`action="view"`, default) or add (`action="add"`) entries in a local JSON-backed schedule of upcoming test windows. |

Tool docstrings in `mcp_server/server.py` are written to be precise about
when/why to call each tool, since tool-selection quality depends
directly on those descriptions.

## The agentic chain (core claim of this project)

The system prompt (`agent/agent_client.py`) instructs the agent to:
1. Always call `check_device_health` before deciding on/running a test.
2. Run a test only when `health_flag` is `warning` or `critical` (or the
   user explicitly asks).
3. On a **failed** test, read `failure_reason` / `recommended_action`
   from the result and respond differently than on a pass:
   `escalate_rerun` (worn device, recommend a re-test) vs.
   `escalate_replace` (critical device, recommend replacement) — not a
   generic "test failed" message.

This chain is exercised for real (not just described) by
`eval/run_eval.py`, which prints the full tool-call trace per question.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# edit .env and set GITHUB_TOKEN to a GitHub personal access token
# (no special scopes needed — GitHub Models free tier)
```

## Running

**Interactive CLI:**
```bash
python main.py
python main.py "Check nvme2n1 and run a test if it needs one"
```

**Eval harness** (runs all cases in `eval/test_questions.json`, prints
the tool-call trace for each, and checks whether the expected tools —
including the full health→test chain for chain cases — were actually
called):
```bash
python -m eval.run_eval
```

**Unit tests** (mock data layer + MCP tool functions, no LLM/network
required):
```bash
python -m pytest tests/ -v
```

**MCP server standalone** (manual smoke test):
```bash
python -m mcp_server.server
```

## Mock devices

| device_id | health_flag | notes |
|---|---|---|
| `nvme0n1` | healthy | low wear, low temp |
| `nvme1n1` | warning | `percentage_used` 61%, some reallocated sectors |
| `nvme2n1` | critical | `percentage_used` 96%, `critical_warning` set, high temp |
| `nvme3n1` | healthy | brand new, near-zero power-on hours |

## Sample tool trace (illustrative)

```
[q4_chain_critical_device_escalation] Check nvme2n1's health, run a power_cycle test if it's warranted, and tell me what to do next.
tool trace:
  [1] CALL check_device_health({'device_id': 'nvme2n1'})
      -> check_device_health: {'device_id': 'nvme2n1', 'health_flag': 'critical', 'percentage_used': 96, ...}
  [2] CALL run_device_test({'device_id': 'nvme2n1', 'test_suite': 'power_cycle'})
      -> run_device_test: {'status': 'fail', 'recommended_action': 'escalate_replace', ...}
final answer:
  nvme2n1 is in critical health (96% wear, critical_warning set) and its power_cycle
  test failed. Recommend flagging this device for replacement, not a re-run.
```

## Testing

- `tests/` — unit tests against the mock data layer and the MCP tool
  functions directly (no MCP transport or LLM needed).
- `eval/test_questions.json` — 10 cases, 4 of which require the full
  health-check → test-decision → run chain.
- `eval/run_eval.py` — runs the live agent against the eval set and
  prints the tool-call trace for each case (tool names, args, order,
  results) — this is the primary artifact for discussing the agentic
  workflow.

## Out of scope

- No real hardware/NVMe controller access — mock data only.
- No Ansible/infra-provisioning layer (a separate skill area, not
  conflated with this project).
- No multi-user state, no persistent memory across sessions.
- No production auth/rate-limiting on the MCP server.
- No UI beyond the CLI demo loop in `main.py`.

## Stack

- MCP server: official `mcp` Python SDK (`FastMCP`), stdio transport.
- Agent: LangGraph `create_react_agent` + `langchain-mcp-adapters`
  `MultiServerMCPClient`.
- LLM: GitHub Models GPT-4o-mini (free tier), via `langchain-openai`
  pointed at GitHub Models' OpenAI-compatible endpoint.
- Local JSON for mock device health/schedule data.
- Runs entirely locally — no Azure, no external hosting.
