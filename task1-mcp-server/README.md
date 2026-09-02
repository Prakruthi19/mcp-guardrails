# Task 1 — Customer Service MCP Server

A runnable MCP server (Python, official `mcp` SDK, stdio transport) exposing:

- `get_customer_record(customer_id)` — `customer_id` must match `CUST-XXXXX` (5 digits).
- `trigger_refund(customer_id, amount, reason, idempotency_key)` — `amount`
  must be a positive float, `reason` must be at least 10 characters,
  `idempotency_key` is a caller-supplied unique token for the refund attempt
  (see idempotency note below).

## Setup & run

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe server.py
```

The server speaks JSON-RPC over stdio — it's meant to be launched by an MCP
client (Claude Desktop, `mcp` CLI, or the test scripts below), not run
interactively.

## Verification scripts

- `test_client.py` — spins up the server via the official `ClientSession` /
  `stdio_client`, calls both tools with valid input, invalid input, and a
  valid-but-nonexistent customer ID, and prints what came back.
- `verify_stdio.py` — talks to the server as a raw subprocess over plain
  stdin/stdout, request-by-request, and asserts every single stdout line is
  parseable JSON-RPC with `jsonrpc: "2.0"`. This is the STDIO isolation check.

```
.venv/Scripts/python.exe test_client.py
.venv/Scripts/python.exe verify_stdio.py
```

## Design notes

**STDIO isolation.** No `print()` (or equivalent) appears anywhere in
`server.py`. All diagnostics go through the stdlib `logging` module,
explicitly configured to write to `sys.stderr`. The SDK's `stdio_server()`
also defends this structurally on top of that: while serving, it diverts
file descriptor 1 so any stray write that *did* bypass logging would land on
stderr, not corrupt the wire — but the code doesn't rely on that safety net.

**Validation → error code mapping.** There are two different situations that
both count as "the tool didn't succeed," and the server maps them onto two
different MCP mechanisms deliberately:

1. **Malformed arguments** — a `customer_id` that doesn't match
   `CUST-XXXXX`, a non-positive `amount`, or a `reason` under 10 characters.
   Each tool validates its arguments against a Pydantic model and, on
   `ValidationError`, raises `mcp.shared.exceptions.MCPError(code=INVALID_PARAMS, ...)`
   (JSON-RPC `-32602`). The SDK passes `MCPError` straight through as a
   top-level JSON-RPC error response instead of wrapping it — this is what
   "reject with standard MCP JSON-RPC error codes" means concretely, and
   `verify_stdio.py` checks it end-to-end.
2. **Well-formed request, business-level failure** — e.g. a syntactically
   valid `CUST-XXXXX` ID that isn't in the (mock, in-memory) customer
   database. This isn't a malformed request, so it's raised as `ToolError`
   instead, which the SDK turns into a normal JSON-RPC *success* response
   with `CallToolResult(isError=True)` — the standard MCP convention for
   "the tool ran but failed," letting an LLM client see the failure message
   and retry/correct.

Basic JSON type mismatches at the transport boundary (e.g. sending `amount`
as a non-numeric string, or omitting a required field entirely) are caught
one layer up by the SDK's own automatic argument-schema validation before
the tool body runs, and surface the same way as case 2 above (`isError:
true` with a descriptive message) rather than as a raw protocol error. This
is intentional and matches the MCP SDK's own convention (see
`mcp/server/mcpserver/tools/base.py::Tool.run`) — the explicit `MCPError`
path in this server is reserved for the semantic validation rules the task
calls out by name (ID format, positive amount, minimum reason length), which
is where a real client integration is most likely to send genuinely bad data.

## Tool annotations & refund idempotency

Both tools declare MCP `ToolAnnotations` so a calling agent (or gateway) can
reason about them without inspecting the implementation:

- `get_customer_record`: `readOnlyHint=true, destructiveHint=false` — it's a
  pure lookup.
- `trigger_refund`: `destructiveHint=true, idempotentHint=true` — it moves
  money, but is safe to retry.

That `idempotentHint=true` is only honest because the tool actually enforces
it, not just because it sounds safe: `idempotency_key` is a required
argument, and the server keeps an in-memory map of
`idempotency_key -> (request fingerprint, result)`.

- Same key + same `customer_id`/`amount`/`reason` → the cached result is
  returned and **no second refund is created**. This is what makes a
  client-side retry (e.g. after a network timeout) safe: the client can't
  tell whether its first attempt actually landed, so it must be able to
  retry with the same key without risking a double refund.
- Same key + *different* arguments → rejected with a JSON-RPC
  `INVALID_PARAMS` error, since that means the key was reused for a
  different logical operation (a caller bug), not a legitimate retry — the
  same pattern Stripe and other payment APIs use for idempotency keys.

The store is an in-memory `dict`, matching the mock `CUSTOMER_DB`; a real
deployment would back it with a persistent, TTL'd store (Redis/SQL) so
retries stay deduped across server restarts, not just within one process.

## Financial guardrails

Before a refund tool like this ever reaches a real payment processor
(Stripe, PayPal, ...), three deterministic checks matter: a per-transaction
cap, a transaction-age limit, and velocity (rolling spend) limits. This
server implements one of the three and deliberately scopes out the other
two:

- **Per-transaction cap — implemented.** `REFUND_AUTO_APPROVE_LIMIT`
  (`$500.00`) is a hard business rule, checked *after* schema validation
  passes. A request above the cap is not rejected as malformed (it's a
  perfectly well-formed request) and not silently processed either — it
  comes back with `status: "escalated"`, a human-readable reason, and no
  `refund_id`, i.e. **no money moves**. It's stored in the same idempotency
  map as approved refunds, so replaying an escalated request's key returns
  the same escalation rather than escalating it again.
- **Transaction-age limit — deliberately not implemented here.** Enforcing
  "only refund transactions within the last 90 days" requires a
  transaction/order reference with a purchase date, and this tool's inputs
  (per the task spec) are just `customer_id`, `amount`, `reason` — there's
  no order being refunded, just an amount against a customer. Adding a fake
  transaction ledger to check an age limit against would be inventing scope
  the task didn't ask for. In a real system this check belongs where the
  transaction data actually lives (an orders/payments service), which this
  refund tool would call out to.
- **Velocity tracking (rolling daily/hourly spend caps) — deliberately not
  implemented here.** This is stateful, cross-call, aggregate rate-limiting
  ("max $2,000/day across the agent's lifetime") — architecturally the same
  problem as Task 4's token-aware sliding-window rate limiter, just dollars
  instead of tokens. It belongs at the gateway/router layer sitting in front
  of (potentially many) tool calls, not duplicated inside one tool.

## A note on the SDK version

`pip install mcp` pulled `mcp==2.1.1`, a notably newer API than the
commonly-documented `mcp` 1.x line (low-level `Server`/`FastMCP` are still
present, but the primary high-level entry point is now `MCPServer` from
`mcp.server.mcpserver`, and its own exception type is `MCPError`, not the
older `McpError`). Its dependency graph also pulls in `httpx2` (the
next-generation successor to `httpx`, same maintainer, published under the
`pydantic` GitHub org) rather than `httpx`. Both were verified against the
installed package metadata before use to rule out a typosquat/supply-chain
issue, given how unusual they looked at first glance.
