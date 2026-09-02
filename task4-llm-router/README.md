# Task 4 — Rate-Limiting & Model Fallback Router

Per-tenant, token-aware sliding window rate limiter (on-disk SQLite),
automatic failover to a secondary provider on 429/3000ms-timeout, and
sanitized standardized error responses.

- `rate_limiter.py` — `SlidingWindowTokenLimiter`. HTTP-agnostic, independently tested.
- `mock_primary_provider.py` / `mock_secondary_provider.py` — controllable
  mocks, no API key needed.
- `router_gateway.py` — reserves tokens, calls primary with a hard timeout,
  fails over, sanitizes every error path.

## Run

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

.venv/Scripts/python.exe -m uvicorn mock_primary_provider:app --port 9200    # terminal 1
.venv/Scripts/python.exe -m uvicorn mock_secondary_provider:app --port 9300  # terminal 2
.venv/Scripts/python.exe -m uvicorn router_gateway:app --port 8070           # terminal 3
```

Call `POST http://127.0.0.1:8070/v1/chat/completions` with an `X-Tenant-Key`
header (`max_tokens` = token cost reserved; `simulate` is a test-only hook).

## Verify

```
.venv/Scripts/python.exe test_rate_limiter.py   # unit tests, no servers needed
.venv/Scripts/python.exe test_router_e2e.py     # live, against all three servers
```

Every e2e test asserts on **timing**, not just status codes: 429 failover
in ~0.14s, timeout failover at ~3.1s (not the primary's full 5s sleep),
rate-limited requests rejected in <50ms (before any upstream call), and a
released reservation proven by a follow-up full-budget request succeeding.

## Two bugs the test suite caught

1. **Concurrency hang.** First version used `BEGIN IMMEDIATE` + `busy_timeout`,
   which only serializes *separate* SQLite connections — it does nothing
   for multiple asyncio tasks interleaving `await`s on one shared
   connection. 50 concurrent requests hung outright. Fixed with an
   `asyncio.Lock` around the critical section — the primitive that actually
   matches "many tasks, one connection."
2. **Verified after the fix:** the same 50-concurrent-request test against a
   budget that fits only 10 now accepts **exactly** 10 — no oversell, no hang.

## Design notes

- **Reserve-then-release**, not count-after-the-fact: tokens reserved
  before calling upstream (using `max_tokens` as the cost estimate),
  released if the request fails on every provider.
- **Failover scoped exactly to the spec**: 429 or timeout only. A primary
  500 returns a sanitized `502` directly — a 5xx isn't obviously a capacity
  problem, so it isn't assumed safe to retry elsewhere.
- **Sanitized by construction**: every error is a fixed hand-written
  `{"error": {"code", "message"}}`, never `str(exc)` or a forwarded
  upstream body. Verified adversarially — the mock primary can return a
  fake leaked traceback, and the test asserts none of that text reaches
  the client.
