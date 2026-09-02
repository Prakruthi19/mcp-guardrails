"""Unit tests for SlidingWindowTokenLimiter, independent of any HTTP layer.
Uses a fresh on-disk SQLite file per test run (deleted at the end).

Run with: .venv/Scripts/python.exe test_rate_limiter.py
"""

from __future__ import annotations

import asyncio
import os

from rate_limiter import RateLimitExceeded, SlidingWindowTokenLimiter

DB_PATH = "test_rate_limiter.db"


async def fresh_limiter(limit_tokens: int, window_seconds: float) -> SlidingWindowTokenLimiter:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    limiter = SlidingWindowTokenLimiter(DB_PATH, limit_tokens=limit_tokens, window_seconds=window_seconds)
    await limiter.start()
    return limiter


async def test_basic_limit() -> None:
    limiter = await fresh_limiter(limit_tokens=1000, window_seconds=60)
    await limiter.try_reserve("tenant-a", 600)
    await limiter.try_reserve("tenant-a", 400)  # exactly at the limit -- should still succeed
    try:
        await limiter.try_reserve("tenant-a", 1)
        raise AssertionError("expected RateLimitExceeded")
    except RateLimitExceeded as exc:
        assert exc.used == 1000 and exc.limit == 1000
    await limiter.close()
    print("[basic_limit] OK")


async def test_per_tenant_isolation() -> None:
    limiter = await fresh_limiter(limit_tokens=1000, window_seconds=60)
    await limiter.try_reserve("tenant-a", 1000)
    # tenant-b has its own independent budget
    await limiter.try_reserve("tenant-b", 1000)
    try:
        await limiter.try_reserve("tenant-a", 1)
        raise AssertionError("expected RateLimitExceeded for tenant-a")
    except RateLimitExceeded:
        pass
    await limiter.close()
    print("[per_tenant_isolation] OK")


async def test_release_refunds_budget() -> None:
    limiter = await fresh_limiter(limit_tokens=1000, window_seconds=60)
    reservation_id = await limiter.try_reserve("tenant-a", 1000)
    try:
        await limiter.try_reserve("tenant-a", 1)
        raise AssertionError("expected RateLimitExceeded before release")
    except RateLimitExceeded:
        pass

    await limiter.release(reservation_id)
    assert await limiter.usage("tenant-a") == 0
    # budget is fully available again
    await limiter.try_reserve("tenant-a", 1000)
    await limiter.close()
    print("[release_refunds_budget] OK")


async def test_sliding_window_eviction() -> None:
    # Short window so the test doesn't need to sleep 60s.
    limiter = await fresh_limiter(limit_tokens=1000, window_seconds=1)
    await limiter.try_reserve("tenant-a", 1000)
    try:
        await limiter.try_reserve("tenant-a", 1)
        raise AssertionError("expected RateLimitExceeded within the window")
    except RateLimitExceeded:
        pass

    await asyncio.sleep(1.2)  # let the 1-second window fully elapse

    # the old reservation should now be evicted -- full budget available again
    await limiter.try_reserve("tenant-a", 1000)
    await limiter.close()
    print("[sliding_window_eviction] OK")


async def test_concurrent_reservations_never_oversell() -> None:
    """The critical correctness test: fire far more concurrent reservation
    attempts than the budget allows, for the same tenant, and confirm the
    limiter never accepts more than the limit in total -- proving the
    BEGIN IMMEDIATE transaction actually serializes the check-then-insert
    instead of letting concurrent requests race past each other.
    """
    limit = 1000
    per_request = 100  # 10 requests would exactly fill it; fire 50 at once
    limiter = await fresh_limiter(limit_tokens=limit, window_seconds=60)

    async def attempt() -> bool:
        try:
            await limiter.try_reserve("tenant-a", per_request)
            return True
        except RateLimitExceeded:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(50)))
    accepted = sum(results)
    total_reserved = accepted * per_request

    assert total_reserved <= limit, f"OVERSOLD: reserved {total_reserved} tokens against a {limit} limit"
    assert accepted == limit // per_request, f"expected exactly {limit // per_request} accepted, got {accepted}"
    actual_usage = await limiter.usage("tenant-a")
    assert actual_usage == total_reserved, f"usage() reports {actual_usage}, expected {total_reserved}"

    await limiter.close()
    print(f"[concurrent_reservations_never_oversell] OK: {accepted}/50 accepted, exactly filling the {limit} budget")


async def main() -> None:
    await test_basic_limit()
    await test_per_tenant_isolation()
    await test_release_refunds_budget()
    await test_sliding_window_eviction()
    await test_concurrent_reservations_never_oversell()

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass

    print("\nAll rate limiter tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
