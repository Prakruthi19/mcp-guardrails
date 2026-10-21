"""End-to-end tests against the real running router_gateway (which itself
calls the real running mock_primary_provider and mock_secondary_provider).

Requires all three servers already running:
  .venv/Scripts/python.exe -m uvicorn mock_primary_provider:app --port 9200
  .venv/Scripts/python.exe -m uvicorn mock_secondary_provider:app --port 9300
  .venv/Scripts/python.exe -m uvicorn router_gateway:app --port 8070

Run with: .venv/Scripts/python.exe test_router_e2e.py
"""

from __future__ import annotations

import time
import uuid

import httpx

GATEWAY_URL = "http://127.0.0.1:8070/v1/chat/completions"


def call(tenant: str, body: dict) -> tuple[int, dict, float]:
    start = time.monotonic()
    resp = httpx.post(GATEWAY_URL, json=body, headers={"X-Tenant-Key": tenant}, timeout=15.0)
    elapsed = time.monotonic() - start
    return resp.status_code, resp.json(), elapsed


def fresh_tenant() -> str:
    return f"tenant-{uuid.uuid4().hex[:8]}"


def test_normal_success() -> None:
    status, data, elapsed = call(fresh_tenant(), {"max_tokens": 50})
    assert status == 200, data
    assert data["provider"] == "primary", data
    print(f"[normal_success] OK provider={data['provider']} elapsed={elapsed:.3f}s")


def test_primary_429_fails_over_fast() -> None:
    status, data, elapsed = call(fresh_tenant(), {"max_tokens": 50, "simulate": "429"})
    assert status == 200, data
    assert data["provider"] == "secondary", data
    # A 429 is immediate, not a timeout -- failover should happen quickly, nowhere near 3s.
    assert elapsed < 1.0, f"failover took {elapsed:.3f}s, expected well under 1s for an immediate 429"
    print(f"[primary_429_fails_over_fast] OK provider={data['provider']} elapsed={elapsed:.3f}s")


def test_primary_timeout_fails_over_at_3s_not_5s() -> None:
    status, data, elapsed = call(fresh_tenant(), {"max_tokens": 50, "simulate": "timeout"})
    assert status == 200, data
    assert data["provider"] == "secondary", data
    # Primary sleeps 5s; the router's budget is 3s. If failover happens at
    # ~3s, the timeout is actually being enforced. If it took ~5s, the
    # router silently waited for the primary instead of enforcing its own
    # budget -- so this bound is the real assertion, not just "it worked".
    assert 2.5 <= elapsed <= 4.0, f"expected failover around 3s, took {elapsed:.3f}s"
    print(f"[primary_timeout_fails_over_at_3s] OK provider={data['provider']} elapsed={elapsed:.3f}s")


def test_both_providers_fail_returns_sanitized_error_and_releases_budget() -> None:
    tenant = fresh_tenant()
    status, data, elapsed = call(
        tenant, {"max_tokens": 50, "simulate": "429", "simulate_secondary": "error"}
    )
    assert status == 502, data
    assert data["error"]["code"] == "ALL_PROVIDERS_UNAVAILABLE", data
    body_text = str(data)
    for leaky in ["Traceback", "postgres://", "DatabaseConnectionError", ".py", "line 42"]:
        assert leaky not in body_text, f"leaked internal detail {leaky!r} in error response: {data}"
    print(f"[both_fail_sanitized_error] OK: {data['error']} (elapsed={elapsed:.3f}s, nothing leaked)")

    # Budget should have been released -- a fresh request for the SAME
    # tenant with the full per-minute limit should still succeed.
    status2, data2, _ = call(tenant, {"max_tokens": 50_000})
    assert status2 == 200, f"expected budget to be released after total failure, got: {data2}"
    print("[both_fail_releases_budget] OK: full budget still available after the failed attempt")


def test_missing_tenant_key() -> None:
    resp = httpx.post(GATEWAY_URL, json={"max_tokens": 10}, timeout=5.0)
    assert resp.status_code == 401, resp.json()
    assert resp.json()["error"]["code"] == "MISSING_TENANT_KEY"
    print("[missing_tenant_key] OK")


def test_rate_limit_enforced_without_calling_primary() -> None:
    tenant = fresh_tenant()
    status1, data1, _ = call(tenant, {"max_tokens": 50_000})
    assert status1 == 200, data1

    status2, data2, elapsed2 = call(tenant, {"max_tokens": 1})
    assert status2 == 429, data2
    assert data2["error"]["code"] == "RATE_LIMIT_EXCEEDED", data2
    # Should be rejected immediately by the limiter, not after a 3s primary
    # call attempt -- proves the reservation check happens before any
    # upstream call, not after.
    assert elapsed2 < 0.5, f"rate-limited request took {elapsed2:.3f}s -- looks like it called upstream anyway"
    print(f"[rate_limit_enforced] OK: second request rejected in {elapsed2:.3f}s without contacting upstream")


if __name__ == "__main__":
    test_normal_success()
    test_primary_429_fails_over_fast()
    test_primary_timeout_fails_over_at_3s_not_5s()
    test_both_providers_fail_returns_sanitized_error_and_releases_budget()
    test_missing_tenant_key()
    test_rate_limit_enforced_without_calling_primary()
    print("\nAll router end-to-end tests passed.")
