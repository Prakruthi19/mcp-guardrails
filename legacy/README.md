# Forward Deployed Engineer Assessment


## [Task 1 — Customer Service MCP Server](./task1-mcp-server)

A runnable MCP server (Python, official `mcp` SDK, stdio transport)
exposing `get_customer_record` and `trigger_refund`, with strict Pydantic
validation, proper JSON-RPC error codes, and verified stdout/stderr
isolation. Beyond the base spec: enforced refund idempotency (a client
retry can never double-refund), a per-transaction auto-approval cap with
escalation instead of silent processing, and MCP tool annotations.

## [Task 2 — MCP Security Gateway Proxy](./task2-mcp-gateway)

An HTTP/JSON-RPC reverse proxy (FastAPI) sitting in front of a mock
downstream MCP server, enforcing mandatory bearer-token authentication and
per-tool-call authorization (`admin_*` tools require the `admin` role,
enforced with the JSON-RPC `-32001` error the task specifies). Beyond the
base spec: structured, credential-safe audit logging of every authorization
decision.

## [Task 3 — LLM Gateway Streaming Guardrail (PII Redaction)](./task3-llm-gateway)

An async streaming proxy that redacts emails, SSNs, and credit card numbers
from an LLM's streamed response in real time, using a bounded-lookback
buffer so PII split across chunk boundaries is still caught without ever
buffering the full response. Credit card matches are Luhn-checksum
validated to avoid false-positive redactions. Includes a documented bug
(and fix) the test suite caught mid-development around open-ended regex
quantifiers matching prematurely on a truncated buffer.

## [Task 4 — Rate-Limiting & Model Fallback Router](./task4-llm-router)

A resilient LLM Gateway router: a per-tenant, token-aware sliding window
rate limiter backed by on-disk SQLite, automatic failover from a primary to
a secondary model provider on a 429 or a 3000ms timeout, and standardized,
sanitized error responses. Includes a documented concurrency bug (and fix)
the test suite caught mid-development around a shared SQLite connection
under concurrent asyncio load.

## Running any task

Each task folder is independent:

```
cd task<N>-...
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Then follow that task's own README for which servers to start and which
test scripts to run.
