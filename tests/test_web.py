"""End-to-end through the HTTP API with the scripted assistant."""

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


def chat(client, who, message):
    r = client.post("/api/chat", json={"message": message}, headers=auth(who))
    assert r.status_code == 200, r.text
    return r.json()


def test_requires_a_known_token(client):
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/api/chat", json={"message": "hi"}, headers={"Authorization": "Bearer nope-nope-nope"}).status_code == 401


def test_outage_story_end_to_end(client):
    # 1. Diagnose: finds the full disk and the crashed nginx.
    r = chat(client, "bob", "Why is the website down on web-01?")
    assert [s["tool"] for s in r["steps"]] == ["host_overview", "list_containers", "container_logs"]
    assert "Cause" in r["reply"] and "/var" in r["reply"]

    # 2. The quick fix doesn't stick while the disk is full.
    r = chat(client, "bob", "Restart nginx on web-01")
    assert r["steps"][0]["status"] == "ok"
    assert "exited again" in r["reply"]

    # 3. Cleanup is dangerous: held for approval.
    r = chat(client, "bob", "Clean up old logs on web-01")
    step = r["steps"][0]
    assert step["status"] == "pending_approval"
    approval_id = step["approval_id"]

    # Bob can't approve; carol (admin) can.
    assert client.post(f"/api/approvals/{approval_id}/approve", headers=auth("bob")).status_code == 403
    r = client.post(f"/api/approvals/{approval_id}/approve", headers=auth("carol"))
    assert r.status_code == 200 and r.json()["status"] == "executed"

    # 4. Now the restart works.
    r = chat(client, "bob", "Restart nginx on web-01")
    assert "running" in r["reply"]

    # Everything is in an intact audit log.
    verify = client.get("/api/audit/verify", headers=auth("alice")).json()
    assert verify["ok"] and verify["checked"] >= 8


def test_viewer_is_blocked_from_changes(client):
    r = chat(client, "alice", "Restart nginx on web-01")
    assert r["steps"][0]["status"] == "denied"
    assert "Blocked" in r["reply"]


def test_redaction_reaches_the_user(client):
    r = chat(client, "alice", "Show the api container logs on api-01")
    assert "sk-live" not in r["reply"] and "jane.doe" not in r["reply"]
    assert "[REDACTED]" in r["reply"]


def test_slurm_questions(client):
    r = chat(client, "bob", "Why is job 4521 pending?")
    assert "gpu-02 is drained" in r["reply"]
    r = chat(client, "bob", "Cancel job 4522")
    assert r["steps"][0]["status"] == "denied"


def test_me_lists_access(client):
    me = client.get("/api/me", headers=auth("alice")).json()
    access = {t["name"]: t["access"] for t in me["tools"]}
    assert access["host_overview"] == "allow"
    assert access["reboot_host"] == "deny"
    assert me["can_approve"] is False


def test_policy_suggestions_are_admin_only(client):
    assert client.get("/api/policy/suggest", headers=auth("bob")).status_code == 403
    assert client.get("/api/policy/suggest", headers=auth("carol")).status_code == 200


def test_health_and_metrics(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    chat(client, "alice", "Why is the website down on web-01?")
    assert "opsguard_tool_calls_total" in client.get("/metrics").text


def test_overview_shows_state_and_per_user_access(client):
    r = client.get("/api/overview", headers=auth("bob"))
    assert r.status_code == 200
    o = r.json()
    web = next(h for h in o["hosts"] if h["host"] == "web-01")
    assert any(c["name"] == "nginx" and c["state"] == "exited" for c in web["containers"])
    assert web["access"]["restart_container"] == "allow"
    assert web["access"]["cleanup_logs"] == "approval"
    db = next(h for h in o["hosts"] if h["host"] == "db-01")
    assert db["protected"] and db["access"]["restart_service"] == "approval"
    assert any("nginx" in i["title"] for i in o["incidents"])
    assert any(n["node"] == "gpu-02" for n in o["slurm"]["nodes"])

    alice = client.get("/api/overview", headers=auth("alice")).json()
    assert next(h for h in alice["hosts"] if h["host"] == "web-01")["access"]["restart_container"] == "deny"


def test_overview_is_not_audited(client):
    before = client.get("/api/audit?limit=500", headers=auth("bob")).json()
    client.get("/api/overview", headers=auth("bob"))
    assert client.get("/api/audit?limit=500", headers=auth("bob")).json() == before


def test_dashboard_buttons_go_through_the_guard(client):
    r = client.post("/api/tools/restart_container", json={"args": {"host": "web-01", "container": "nginx"}}, headers=auth("alice"))
    assert r.status_code == 200 and r.json()["status"] == "denied"

    r = client.post("/api/tools/container_logs", json={"args": {"host": "api-01", "container": "api"}}, headers=auth("alice"))
    body = r.json()
    assert body["status"] == "ok" and body["redactions"] == 4
    assert "sk-live" not in str(body["result"])

    r = client.post("/api/tools/cleanup_logs", json={"args": {"host": "web-01", "path": "/var/log/app"}}, headers=auth("bob"))
    assert r.json()["status"] == "pending_approval"

    entries = client.get("/api/audit?limit=500", headers=auth("bob")).json()
    assert [e["event"] for e in entries[-3:]] == ["denied", "executed", "approval_requested"]
    assert all(e["source"] == "web" for e in entries[-3:])


def test_demo_reset_expires_pending_approvals(client):
    r = client.post("/api/tools/cleanup_logs", json={"args": {"host": "web-01", "path": "/var/log/app"}}, headers=auth("bob"))
    approval_id = r.json()["approval_id"]
    client.post("/api/demo/reset", headers=auth("bob"))
    assert client.get("/api/approvals?status=pending", headers=auth("carol")).json() == []
    assert client.post(f"/api/approvals/{approval_id}/approve", headers=auth("carol")).status_code == 409


def test_landing_console_and_policy(client):
    assert "Without the risk" in client.get("/").text
    assert 'id="page-overview"' in client.get("/app").text
    assert client.get("/api/policy").status_code == 401
    p = client.get("/api/policy", headers=auth("alice")).json()
    assert p["roles"]["viewer"]["change"] == "deny" and "db-01" in p["protected_targets"]
    assert any(t["name"] == "cleanup_logs" and t["risk"] == "dangerous" for t in p["tools"])
