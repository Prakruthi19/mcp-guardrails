"""SQLite-backed state: the audit log, the approval queue and rate limits.

One aiosqlite connection per process, guarded by one asyncio.Lock. The lock
is not optional: SQLite's own locking (BEGIN IMMEDIATE, busy_timeout) only
serializes *separate connections*. Many asyncio tasks interleaving awaits on
the *same* connection look like a single client to SQLite, and the earlier
version of the rate limiter (legacy/task4-llm-router) hung under 50
concurrent requests for exactly that reason. The web server and the MCP
server are separate processes with separate connections, which is the case
SQLite's own locking does handle (WAL mode + busy_timeout).

Audit log integrity: each entry stores the SHA-256 of the previous entry's
hash plus its own content. Editing or deleting any past row breaks every
hash after it, and `verify_audit()` reports the first broken entry. This
does not stop someone with write access to the file from rewriting the
whole chain - shipping the log to a separate system does that - but it
makes quiet, after-the-fact edits detectable.

Workspaces (tenants): audit entries and approvals carry a `workspace`
column. Each workspace has its own hash chain, so one tenant's log can be
verified - and handed to that tenant - without any other tenant's entries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from opsguard.workspace import DEFAULT as DEFAULT_WORKSPACE

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    event TEXT NOT NULL,
    tool TEXT,
    args TEXT,
    decision TEXT,
    detail TEXT,
    approval_id INTEGER,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    workspace TEXT NOT NULL DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    requester TEXT NOT NULL,
    requester_role TEXT NOT NULL,
    source TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    result TEXT,
    workspace TEXT NOT NULL DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_bucket_at ON rate_events(bucket, at);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_audit_ws ON audit(workspace, id);
CREATE INDEX IF NOT EXISTS idx_approvals_ws ON approvals(workspace, id);
"""

_AUDIT_FIELDS = ("ts", "actor", "role", "source", "event", "tool", "args", "decision", "detail", "approval_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry_hash(prev_hash: str, entry: dict[str, Any], workspace: str = DEFAULT_WORKSPACE) -> str:
    fields = {k: entry.get(k) for k in _AUDIT_FIELDS}
    # The workspace is hashed too, so an entry can't be moved to another
    # tenant's chain. It is left out for the default workspace so chains
    # written before workspaces existed still verify.
    if workspace != DEFAULT_WORKSPACE:
        fields["workspace"] = workspace
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + payload).encode()).hexdigest()


class Store:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.executescript(_SCHEMA)
        # Databases created before workspaces existed: add the column in place.
        for table in ("audit", "approvals"):
            async with self._db.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row["name"] for row in await cur.fetchall()}
            if "workspace" not in columns:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN workspace TEXT NOT NULL DEFAULT 'default'")
        await self._db.executescript(_INDEXES)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "call start() first"
        return self._db

    # -- audit ---------------------------------------------------------------

    async def audit(
        self,
        *,
        actor: str,
        role: str,
        source: str,
        event: str,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        decision: str | None = None,
        detail: str | None = None,
        approval_id: int | None = None,
        workspace: str = DEFAULT_WORKSPACE,
    ) -> int:
        entry = {
            "ts": _now(),
            "actor": actor,
            "role": role,
            "source": source,
            "event": event,
            "tool": tool,
            "args": json.dumps(args, sort_keys=True) if args is not None else None,
            "decision": decision,
            "detail": detail,
            "approval_id": approval_id,
        }
        async with self._lock:
            # BEGIN IMMEDIATE takes the write lock before reading the last
            # hash, so another process can't append between our read and
            # our insert and fork the chain.
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT hash FROM audit WHERE workspace = ? ORDER BY id DESC LIMIT 1", (workspace,)
                ) as cur:
                    row = await cur.fetchone()
                prev_hash = row["hash"] if row else GENESIS_HASH
                entry_hash = _entry_hash(prev_hash, entry, workspace)
                cur = await self.db.execute(
                    f"INSERT INTO audit ({', '.join(_AUDIT_FIELDS)}, prev_hash, hash, workspace) "
                    f"VALUES ({', '.join('?' * (len(_AUDIT_FIELDS) + 3))})",
                    (*[entry[k] for k in _AUDIT_FIELDS], prev_hash, entry_hash, workspace),
                )
                await self.db.execute("COMMIT")
                return int(cur.lastrowid)
            except BaseException:
                if self.db.in_transaction:
                    await self.db.execute("ROLLBACK")
                raise

    async def audit_entries(self, after: int = 0, limit: int = 200, workspace: str | None = None) -> list[dict[str, Any]]:
        """Entries after id `after`, oldest first. `workspace=None` means all
        workspaces (for the CLI); the web API always passes the caller's."""
        where, params = _ws_filter("id > ?", [after], workspace)
        async with self.db.execute(f"SELECT * FROM audit WHERE {where} ORDER BY id ASC LIMIT ?", (*params, limit)) as cur:
            rows = await cur.fetchall()
        return [_audit_row(r) for r in rows]

    async def recent_audit(self, limit: int = 100, workspace: str | None = None) -> list[dict[str, Any]]:
        where, params = _ws_filter("1 = 1", [], workspace)
        async with self.db.execute(f"SELECT * FROM audit WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)) as cur:
            rows = await cur.fetchall()
        return [_audit_row(r) for r in reversed(rows)]

    async def verify_audit(self, workspace: str | None = None) -> dict[str, Any]:
        """Check one workspace's chain, or every chain when `workspace` is None."""
        prev: dict[str, str] = {}
        checked = 0
        where, params = _ws_filter("1 = 1", [], workspace)
        async with self.db.execute(f"SELECT * FROM audit WHERE {where} ORDER BY id ASC", params) as cur:
            async for row in cur:
                ws = row["workspace"]
                prev_hash = prev.get(ws, GENESIS_HASH)
                entry = {k: row[k] for k in _AUDIT_FIELDS}
                if row["prev_hash"] != prev_hash or _entry_hash(prev_hash, entry, ws) != row["hash"]:
                    return {"ok": False, "checked": checked, "broken_at": row["id"]}
                prev[ws] = row["hash"]
                checked += 1
        return {"ok": True, "checked": checked, "broken_at": None}

    # -- approvals -----------------------------------------------------------

    async def create_approval(
        self, *, requester: str, requester_role: str, source: str, tool: str, args: dict[str, Any], reason: str,
        workspace: str = DEFAULT_WORKSPACE,
    ) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO approvals (created_at, requester, requester_role, source, tool, args, reason, status, workspace) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (_now(), requester, requester_role, source, tool, json.dumps(args, sort_keys=True), reason, workspace),
            )
            return int(cur.lastrowid)

    async def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        async with self.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)) as cur:
            row = await cur.fetchone()
        return _approval_row(row) if row else None

    async def list_approvals(
        self, status: str | None = None, limit: int = 100, workspace: str | None = None
    ) -> list[dict[str, Any]]:
        where, params = _ws_filter("status = ?" if status else "1 = 1", [status] if status else [], workspace)
        async with self.db.execute(f"SELECT * FROM approvals WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)) as cur:
            rows = await cur.fetchall()
        return [_approval_row(r) for r in rows]

    async def claim_approval(self, approval_id: int, decided_by: str, status: str) -> bool:
        """Move a pending approval to `status`. Returns False if it was no
        longer pending - the conditional UPDATE is what stops two approvers
        clicking at the same moment from running the action twice."""
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (status, decided_by, _now(), approval_id),
            )
            return cur.rowcount == 1

    async def finish_approval(self, approval_id: int, status: str, result: Any) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE approvals SET status = ?, result = ? WHERE id = ?",
                (status, json.dumps(result), approval_id),
            )

    async def expire_pending(self, decided_by: str, workspace: str | None = None) -> int:
        """Close pending approvals without running them (used when the demo
        resets, so stale requests can't run against the fresh state)."""
        where, params = _ws_filter("status = 'pending'", [], workspace)
        async with self._lock:
            cur = await self.db.execute(
                f"UPDATE approvals SET status = 'expired', decided_by = ?, decided_at = ? WHERE {where}",
                (decided_by, _now(), *params),
            )
            return cur.rowcount

    # -- rate limits ---------------------------------------------------------

    async def hit_rate_limit(self, bucket: str, limit: int, window_seconds: int = 60) -> bool:
        """Record one event in `bucket`. Returns True if it is within the
        limit, False (and records nothing) if the sliding window is full."""
        now = time.time()
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute("DELETE FROM rate_events WHERE bucket = ? AND at < ?", (bucket, now - window_seconds))
                async with self.db.execute("SELECT COUNT(*) AS n FROM rate_events WHERE bucket = ?", (bucket,)) as cur:
                    row = await cur.fetchone()
                if row["n"] >= limit:
                    await self.db.execute("ROLLBACK")
                    return False
                await self.db.execute("INSERT INTO rate_events (bucket, at) VALUES (?, ?)", (bucket, now))
                await self.db.execute("COMMIT")
                return True
            except BaseException:
                if self.db.in_transaction:
                    await self.db.execute("ROLLBACK")
                raise


def _ws_filter(where: str, params: list[Any], workspace: str | None) -> tuple[str, list[Any]]:
    if workspace is None:
        return where, params
    return f"{where} AND workspace = ?", [*params, workspace]


def _audit_row(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["args"] = json.loads(d["args"]) if d["args"] else None
    return d


def _approval_row(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["args"] = json.loads(d["args"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d
