"""Workspaces (tenants) are isolated: servers, approvals and audit logs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opsguard.web.app import create_app


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as c:
        yield c


def auth(name):
    return {"Authorization": f"Bearer demo-{name}"}


def web01(client, who):
    hosts = client.get("/api/overview", headers=auth(who)).json()["hosts"]
    return next(h for h in hosts if h["host"] == "web-01")


def test_each_workspace_has_its_own_servers(client):
    r = client.post("/api/tools/cleanup_logs", json={"args": {"host": "web-01", "path": "/var/log/app"}}, headers=auth("bob"))
    client.post(f"/api/approvals/{r.json()['approval_id']}/approve", headers=auth("carol"))

    acme_var = next(d for d in web01(client, "bob")["disks"] if d["mount"] == "/var")
    globex_var = next(d for d in web01(client, "dave")["disks"] if d["mount"] == "/var")
    assert acme_var["used_percent"] < 60   # Acme freed its disk...
    assert globex_var["used_percent"] == 97  # ...Globex's web-01 is untouched


def test_approvals_are_invisible_and_unapprovable_across_workspaces(client):
    r = client.post("/api/tools/cleanup_logs", json={"args": {"host": "web-01", "path": "/var/log/app"}}, headers=auth("bob"))
    approval_id = r.json()["approval_id"]

    assert client.get("/api/approvals", headers=auth("erin")).json() == []
    # Erin is an admin, but in Globex: the request looks like it doesn't exist.
    resp = client.post(f"/api/approvals/{approval_id}/approve", headers=auth("erin"))
    assert resp.status_code == 409 and "no approval request" in resp.json()["detail"]
    assert [a["id"] for a in client.get("/api/approvals", headers=auth("carol")).json()] == [approval_id]
    assert client.post(f"/api/approvals/{approval_id}/approve", headers=auth("carol")).json()["status"] == "executed"


def test_audit_logs_and_chains_are_per_workspace(client):
    client.post("/api/tools/restart_container", json={"args": {"host": "web-01", "container": "nginx"}}, headers=auth("bob"))
    client.post("/api/tools/list_hosts", json={"args": {}}, headers=auth("dave"))
    client.post("/api/tools/host_overview", json={"args": {"host": "api-01"}}, headers=auth("alice"))

    acme = client.get("/api/audit", headers=auth("carol")).json()
    globex = client.get("/api/audit", headers=auth("erin")).json()
    assert {e["actor"] for e in acme} == {"bob", "alice"}
    assert {e["actor"] for e in globex} == {"dave"}
    # Interleaved writes still form two unbroken chains.
    assert acme[1]["prev_hash"] == acme[0]["hash"]
    assert client.get("/api/audit/verify", headers=auth("carol")).json() == {"ok": True, "checked": 2, "broken_at": None}
    assert client.get("/api/audit/verify", headers=auth("erin")).json() == {"ok": True, "checked": 1, "broken_at": None}


def test_demo_reset_only_touches_your_workspace(client):
    client.post("/api/tools/remove_container", json={"args": {"host": "web-01", "container": "app"}}, headers=auth("carol"))
    r = client.post("/api/tools/cleanup_logs", json={"args": {"host": "web-01", "path": "/var/log/app"}}, headers=auth("dave"))
    client.post("/api/demo/reset", headers=auth("bob"))
    # Globex's pending request survives Acme's reset.
    pending = client.get("/api/approvals?status=pending", headers=auth("erin")).json()
    assert [a["id"] for a in pending] == [r.json()["approval_id"]]


async def test_old_databases_are_migrated(tmp_path):
    import aiosqlite

    from opsguard.safety.store import Store

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:  # the pre-workspace schema
        await db.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, "
                         "role TEXT NOT NULL, source TEXT NOT NULL, event TEXT NOT NULL, tool TEXT, args TEXT, decision TEXT, "
                         "detail TEXT, approval_id INTEGER, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)")
        await db.commit()
    store = Store(str(path))
    await store.start()
    await store.audit(actor="a", role="admin", source="t", event="executed", workspace="acme")
    assert (await store.verify_audit())["ok"]
    await store.close()
