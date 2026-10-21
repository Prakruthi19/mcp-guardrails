# Task 1 — Customer Service MCP Server

Python MCP server (official `mcp` SDK, stdio transport) exposing:

- `get_customer_record(customer_id)` — must match `CUST-XXXXX`.
- `trigger_refund(customer_id, amount, reason, idempotency_key)` — `amount` > 0,
  `reason` ≥ 10 chars, `idempotency_key` caller-supplied (see below).

## Run

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe server.py
```

Speaks JSON-RPC over stdio — launched by an MCP client, not run interactively.

## Verify

```
.venv/Scripts/python.exe test_client.py    # valid/invalid input, not-found case
.venv/Scripts/python.exe verify_stdio.py   # asserts stdout is 100% clean JSON-RPC
```

## Design notes

**Error mapping.** Malformed args (bad ID format, non-positive amount, short
reason) raise `MCPError(INVALID_PARAMS)` → a real JSON-RPC `-32602` error. A
well-formed request for a customer that doesn't exist raises `ToolError` →
a normal JSON-RPC success with `isError: true` (standard MCP convention —
the request wasn't broken, the answer was just "no"). stdout carries only
JSON-RPC; all logging goes to `sys.stderr`.

**Refund idempotency.** `idempotency_key` is required. Same key + same args
→ returns the cached result, no second refund created. Same key + different
args → rejected (`-32602`), matching Stripe's idempotency-key pattern. This
is what makes `trigger_refund`'s `idempotentHint: true` MCP tool annotation
actually true, not just asserted.

**Auto-approval cap.** Refunds over `$500` come back `status: "escalated"`
with no `refund_id` — no money moves, but it's not an error either (the
request was valid). A transaction-age limit and velocity/rolling-spend caps
were deliberately left out: the former needs a transaction ledger this
tool's inputs don't have, and the latter is architecturally Task 4's
problem (dollars instead of tokens), not this tool's.

**SDK note.** `pip install mcp` resolved to `mcp==2.1.1` — a notably newer
API (`MCPServer`, `MCPError`) than the commonly-documented 1.x line, pulling
in `httpx2` instead of `httpx`. Verified both against package metadata
before trusting them (legitimate, not a typosquat) since the version gap
was large enough to be worth checking rather than assuming.
