"""Token-aware sliding window rate limiter, backed by on-disk SQLite.

Sliding window (not fixed window): a fixed window resets its counter at a
clock boundary, which lets a tenant burst up to 2x the limit right around
the boundary (max out the last second of one window, then immediately max
out the first second of the next). A sliding window instead looks at "how
many tokens has this tenant used in the last WINDOW_SECONDS, as of right
now" -- implemented here as a sliding window *log*: one row per reservation,
with old rows aged out on every check.

Reserve-then-release, not just "check and count": tokens are reserved
*before* the call to an upstream provider is even made (so N concurrent
requests can't each individually pass a check that's only valid one-at-a-
time and collectively blow through the budget), and explicitly released if
the request ultimately fails everywhere (so a tenant isn't charged budget
for a request that never actually got a completion). Each reservation is
its own row with its own id, released by id -- not by decrementing a
running total -- so concurrent reserve/release calls for the same tenant
can't clobber each other.

Concurrency safety: this gateway uses a single shared aiosqlite connection
(one Python process, one on-disk file). `BEGIN IMMEDIATE` + `busy_timeout`
alone does NOT make the check-then-insert section safe here -- that
mechanism serializes *separate* SQLite connections/processes contending for
the file lock, but does nothing about multiple asyncio tasks interleaving
`await` calls on the *same* connection object, which is what actually
happens under concurrent requests: task B's own `BEGIN IMMEDIATE` can start
on the shared connection while task A's transaction is still open, mid-
`await`, which isn't a race that resolves via SQLite's locking -- both
transactions are on the one connection SQLite thinks is a single client.
An earlier version of this module hit exactly that: it hung under
concurrent load rather than raising or oversold. The actual fix is an
`asyncio.Lock` around the whole critical section, serializing it at the
asyncio-task level, which is the primitive that matches "many tasks, one
connection" -- BEGIN IMMEDIATE is kept underneath it as the right
transaction type for a future multi-connection deployment, but the lock is
what makes this correct today. Exercised in test_rate_limiter.py by firing
many concurrent reservations at once and asserting total accepted tokens
never exceeds the limit.
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite

DEFAULT_LIMIT_TOKENS = 50_000
DEFAULT_WINDOW_SECONDS = 60


class RateLimitExceeded(Exception):
    def __init__(self, tenant_key: str, requested: int, used: int, limit: int) -> None:
        self.tenant_key = tenant_key
        self.requested = requested
        self.used = used
        self.limit = limit
        super().__init__(f"tenant {tenant_key!r} requested {requested}, already used {used}/{limit} in window")


class SlidingWindowTokenLimiter:
    def __init__(
        self,
        db_path: str,
        limit_tokens: int = DEFAULT_LIMIT_TOKENS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._limit_tokens = limit_tokens
        self._window_seconds = window_seconds
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._db = await aiosqlite.connect(self._db_path, isolation_level=None)
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_key TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                reserved_at REAL NOT NULL
            )
            """
        )
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_tenant_time ON reservations(tenant_key, reserved_at)")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def try_reserve(self, tenant_key: str, tokens: int) -> int:
        """Attempt to reserve `tokens` for `tenant_key`. Returns the
        reservation id on success (pass it to release() if the request
        later fails). Raises RateLimitExceeded if this would push the
        tenant's trailing-window usage over the limit.
        """
        assert self._db is not None, "call start() first"
        now = time.time()
        cutoff = now - self._window_seconds

        async with self._lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                await self._db.execute(
                    "DELETE FROM reservations WHERE tenant_key = ? AND reserved_at < ?", (tenant_key, cutoff)
                )

                async with self._db.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM reservations WHERE tenant_key = ?", (tenant_key,)
                ) as cursor:
                    row = await cursor.fetchone()
                    used = row[0] if row else 0

                if used + tokens > self._limit_tokens:
                    await self._db.execute("ROLLBACK")
                    raise RateLimitExceeded(tenant_key, tokens, used, self._limit_tokens)

                cursor = await self._db.execute(
                    "INSERT INTO reservations (tenant_key, tokens, reserved_at) VALUES (?, ?, ?)",
                    (tenant_key, tokens, now),
                )
                reservation_id = cursor.lastrowid
                await self._db.execute("COMMIT")
                return reservation_id
            except BaseException:
                try:
                    await self._db.execute("ROLLBACK")
                except aiosqlite.OperationalError:
                    pass  # nothing to roll back (we already committed or rolled back above)
                raise

    async def release(self, reservation_id: int) -> None:
        """Refund a reservation -- used when a request that reserved tokens
        ultimately failed on every provider, so it shouldn't count against
        the tenant's budget."""
        assert self._db is not None, "call start() first"
        async with self._lock:
            await self._db.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))

    async def usage(self, tenant_key: str) -> int:
        """Current trailing-window usage for a tenant, for observability/tests."""
        assert self._db is not None, "call start() first"
        cutoff = time.time() - self._window_seconds
        async with self._db.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM reservations WHERE tenant_key = ? AND reserved_at >= ?",
            (tenant_key, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
