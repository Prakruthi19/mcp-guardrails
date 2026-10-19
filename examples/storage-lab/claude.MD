# Storage Device Test Automation MCP Agent

## What this is

An MCP-based AI agent that automates storage device (NVMe SSD) test
execution and scheduling by calling tools exposed via an MCP server —
demonstrating hands-on MCP protocol + agentic test-automation workflow
experience, aimed directly at test-automation/lab-infra roles.

## Problem this solves

Test automation for storage devices typically means either (a) hardcoded
scripts triggering fixed test suites, or (b) manual triage of failures.
There's no standard way for an AI agent to reason about *which* test to
run, *when* to schedule it, and *how* to interpret device health signals
to decide next steps. This agent uses MCP to expose device-test
operations as tools an LLM agent can call, chain, and reason over —
closer to how a real test-automation engineer thinks (check health ->
decide what to test -> schedule/run -> triage failure).

## Architecture

```
User/scheduler query -> LangGraph ReAct agent -> MCP client -> MCP server -> tools
                              |
                        GitHub Models (GPT-4o-mini, free tier)
```

Two processes: MCP server (tool host, stdio transport) and agent client
(LangGraph, via `langchain-mcp-adapters`). Same pattern as a real MCP
deployment integrating an agent with lab/testing tools.

## Tools (MCP server)

1. `check_device_health(device_id)` — returns simulated SMART-style
   health data (temperature, wear level / endurance used %, reallocated
   sector count, power-on hours). No physical device required — build a
   local JSON/mock data layer keyed by device_id, structured to look
   like real NVMe SMART log output so the shape is realistic.
2. `run_device_test(device_id, test_suite)` — triggers a test run
   (simulate execution: sleep briefly, return pass/fail + a result
   summary). `test_suite` should be an enum-like string, e.g.
   `"read_write_endurance"`, `"latency_benchmark"`, `"power_cycle"`.
3. `get_test_schedule(device_id)` — returns/manages a simple local
   schedule (JSON-backed) of upcoming test windows for a device.

**Required multi-tool chain:** `check_device_health` -> decide (based on
wear level or health flags) whether a test is warranted ->
`run_device_test` -> if failed, an escalation/triage response. This
chain (health check informing the test decision) is the core
"agentic workflow" claim for this project — it must actually work.

## Stack (all free, no paid infra)

- MCP server: official `mcp` Python SDK, stdio transport
- Agent: LangGraph `create_react_agent` + `langchain-mcp-adapters`
  `MultiServerMCPClient`
- LLM: GitHub Models GPT-4o-mini endpoint (`GITHUB_TOKEN` env var)
- Local JSON for mock device health/schedule data — no physical
  hardware, no paid cloud device-management API
- No Azure, no external hosting — runs locally

## Non-negotiable requirements

- **Failure handling is required.** `run_device_test` must handle a
  simulated failure case explicitly (not just always return pass) and
  the agent's response to a failed test must differ meaningfully from
  a passed one (e.g. suggest re-run, flag for escalation).
- **The health-check -> test-decision -> run chain must actually
  execute**, not just be described in the README. This is the project's
  core agentic-workflow claim.
- **Be explicit and honest that device data is simulated/mocked**, not
  pulled from real physical NVMe hardware — say this plainly in the
  README. Do not imply real hardware access. The realism is in the data
  *shape* (matching real SMART attribute names/structure), not in the
  data being genuine.
- **MCP `list_tools()` descriptions must be precise** — tool selection
  quality depends on this directly.

## What NOT to build (explicitly out of scope)

- No real hardware/NVMe controller access — mock data only, clearly labeled
- No Ansible/infra-provisioning layer in this project (that's a separate,
  real skill area to speak to independently in the interview — don't
  conflate it with this agent)
- No multi-user state, no persistent memory across sessions
- No production auth/rate-limiting on the MCP server
- No UI beyond a CLI Q&A/demo loop in `main.py`

## Testing

- `tests/` — unit tests per tool using the mock data layer
- `eval/test_questions.json` — ~10 cases, at least 3 requiring the
  health-check -> test-decision -> run chain
- `eval/run_eval.py` — runs the agent against the eval set, prints the
  tool-call trace (which tools, what order, what args) — this trace is
  the interview talking point, make sure it's actually logged/printed

## Context / origin story (for README, do not oversell)

Grounded in real experience maintaining AWS-based Slurm HPC clusters and
lab-adjacent infrastructure at DOT Consulting, where test/job scheduling
and health monitoring of compute resources is routine work. This project
extends that real experience into an agentic pattern using MCP, applied
to storage-device test automation as a natural adjacent domain — not
built around or dependent on any other in-progress project.

## Style / honesty constraints

- No "production-ready" / "enterprise-grade" language — this is a
  portfolio/demo project and should read as one.
- Be upfront in the README that this project was scoped and re-scoped
  quickly under time pressure — better to say "MVP demonstrating the
  pattern" than to imply more polish than exists.
- Keep commit history incremental (scaffold -> mock data -> tools ->
  agent wiring -> eval -> tests), not one giant commit.