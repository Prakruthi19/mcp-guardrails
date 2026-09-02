# Task 4 — Rate-Limiting & Model Fallback Router

A resilient model-routing module for an LLM Gateway: a per-tenant,
token-aware sliding window rate limiter backed by on-disk SQLite, automatic
failover from a primary to a secondary model provider on 429 or a 3000ms
timeout, and standardized, sanitized error responses.

- `rate_limiter.py` — `SlidingWindowTokenLimiter`, the actual rate-limiting
  logic. HTTP-agnostic, independently tested.
- `mock_primary_provider.py` / `mock_secondary_provider.py` — controllable
  mock upstream providers (no API key needed) so every failure mode can be
  triggered deterministically.
- `router_gateway.py` — the gateway: reserves tokens, calls the primary with
  a hard timeout, fails over on 429/timeout, sanitizes every error path.

## Setup & run

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# terminal 1
.venv/Scripts/python.exe -m uvicorn mock_primary_provider:app --port 9200

# terminal 2
.venv/Scripts/python.exe -m uvicorn mock_secondary_provider:app --port 9300

# terminal 3
.venv/Scripts/python.exe -m uvicorn router_gateway:app --port 8070
```

Clients call `POST http://127.0.0.1:8070/v1/chat/completions` with an
`X-Tenant-Key` header and a JSON body (`max_tokens` is read as the token
cost to reserve against that tenant's budget; a `simulate` field is a
test-only hook the mock primary understands — see its docstring).

## Verification

```
.venv/Scripts/python.exe test_rate_limiter.py   # pure unit tests, no servers needed
.venv/Scripts/python.exe test_router_e2e.py     # live, against all three running servers
```

Every end-to-end test asserts on **timing**, not just status codes — because
for this task, "it eventually returned something" isn't proof the timeout
or the rate limiter actually work:

- 429 failover completes in ~0.14s (immediate, not a timeout).
- Timeout failover completes at **~3.1s**, not the primary's full 5s sleep —
  proving the 3000ms budget is actually enforced via cancellation, not
  silently waited out.
- A rate-limited request is rejected in **under 50ms** — proving the
  reservation check happens before any upstream call is even attempted, not
  after a failed one.
- A request that fails on both providers gets a sanitized `502`, and a
  fresh request for the same tenant with the *full* per-minute budget still
  succeeds — proving the reservation was actually released, not just that
  an error was returned.

## Two bugs the test suite caught during development

Both are real, and both are documented in full in `rate_limiter.py`'s
module docstring — summarized here:

1. **A hang under concurrent load.** The first version wrapped the
   check-then-insert critical section in a `BEGIN IMMEDIATE` SQLite
   transaction with a `busy_timeout`, reasoning that this would serialize
   concurrent reservation attempts. It doesn't — that mechanism serializes
   *separate* SQLite connections/processes contending for the file lock. This
   gateway uses one shared `aiosqlite` connection for the whole process, and
   multiple asyncio tasks calling it concurrently just interleave their
   `await`s on that single connection — a second task's `BEGIN IMMEDIATE`
   can start while a first task's transaction is still open, mid-`await`,
   which SQLite's locking doesn't protect against at all (from SQLite's
   point of view, it's all one client). Firing 50 concurrent reservations at
   it hung outright rather than raising or overselling. Fixed with an
   `asyncio.Lock` around the whole critical section — the primitive that
   actually matches "many tasks, one connection" (`BEGIN IMMEDIATE` is kept
   underneath it, since it's still the right transaction type if this ever
   moves to multiple connections/processes).
2. **Verified, not assumed, after the fix:** the same 50-concurrent-request
   test against a budget that only fits 10 of them now accepts **exactly**
   10 — no oversell, no hang — which is what actually proves the limiter is
   correct under load rather than just correct in a single-threaded test.

## Design decisions worth stating explicitly

- **Reserve-then-release, not count-after-the-fact.** Tokens are reserved
  *before* a provider is ever called (using `max_tokens` as the cost
  estimate — the standard conservative choice, since it's the worst-case
  token spend for the request). If the request ultimately fails on every
  provider, the reservation is explicitly released, so a tenant isn't
  charged budget for a request that never produced a completion. A
  production system would likely also reconcile the reservation against the
  *actual* token usage a successful response reports, rather than always
  keeping the `max_tokens` estimate — left out here to keep scope to what
  the task actually asks for.
- **Failover is scoped exactly to what the task specifies: 429 or timeout.**
  A primary 500 (or any other non-429 error) is surfaced directly as a
  sanitized `502 UPSTREAM_ERROR` rather than triggering failover — the task
  explicitly names only two triggers, and a 5xx is a provider-side problem,
  not obviously a capacity one, so silently retrying it against a second
  provider isn't assumed safe by default. A real deployment might reasonably
  broaden this policy; flagged here rather than assumed.
- **Errors are sanitized by construction, not by scrubbing.** Every failure
  path returns exactly `{"error": {"code": ..., "message": ...}}` built from
  a fixed, hand-written message — never `str(exc)`, never a forwarded
  upstream response body. The actual exception/stack trace goes to
  `logger.exception(...)` (server-side only). `mock_primary_provider.py`'s
  `simulate: "error"` mode deliberately returns a fake-leaky payload (with a
  fake traceback and an internal hostname in it) specifically so
  `test_router_e2e.py` can assert none of that text ever reaches the client.
