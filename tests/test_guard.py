from __future__ import annotations

import asyncio

import pytest

from opsguard import workspace
from opsguard.runtime import build_runtime
from tests.conftest import demo_config


def acme_hosts(runtime):
    # The demo users live in the 'acme' workspace, which has its own servers.
    with workspace.use("acme"):
        return runtime.demo.hosts


async def test_read_runs_and_is_audited(runtime, users):
    out = await runtime.guard.call(users["alice"], "host_overview", {"host": "web-01"}, source="test")
    assert out.status == "ok"
    assert out.result["warnings"] == ["disk /var is almost full"]
    entries = await runtime.store.recent_audit()
    assert entries[-1]["event"] == "executed"
    assert entries[-1]["actor"] == "alice"


async def test_denied_call_never_reaches_the_tool(runtime, users):
    before = acme_hosts(runtime)["web-01"]["containers"]["app"]["status"]
    out = await runtime.guard.call(users["alice"], "remove_container", {"host": "web-01", "container": "app"}, source="test")
    assert out.status == "denied"
    assert "app" in acme_hosts(runtime)["web-01"]["containers"]
    assert acme_hosts(runtime)["web-01"]["containers"]["app"]["status"] == before


async def test_bad_arguments_are_rejected(runtime, users):
    out = await runtime.guard.call(users["bob"], "container_logs", {"host": "web-01", "container": "nginx", "lines": "all"}, source="test")
    assert out.status == "error"
    out = await runtime.guard.call(users["bob"], "host_overview", {"host": "web-01", "extra": 1}, source="test")
    assert out.status == "error"
    out = await runtime.guard.call(users["bob"], "no_such_tool", {}, source="test")
    assert out.status == "error"


async def test_approval_flow_runs_as_requester_after_admin_approves(runtime, users):
    bob, carol = users["bob"], users["carol"]
    out = await runtime.guard.call(bob, "cleanup_logs", {"host": "web-01", "path": "/var/log/app"}, source="test")
    assert out.status == "pending_approval"
    assert acme_hosts(runtime)["web-01"]["log_dirs"]["/var/log/app"] > 20  # nothing deleted yet

    with pytest.raises(PermissionError):
        await runtime.guard.approve(bob, out.approval_id)  # operators can't approve

    result = await runtime.guard.approve(carol, out.approval_id)
    assert result["status"] == "executed"
    assert acme_hosts(runtime)["web-01"]["log_dirs"]["/var/log/app"] < 1

    with pytest.raises(LookupError):
        await runtime.guard.approve(carol, out.approval_id)  # can't run twice

    events = [e["event"] for e in await runtime.store.recent_audit()]
    assert events[-3:] == ["approval_requested", "approval_granted", "executed"]


async def test_admin_cannot_approve_own_request(runtime, users):
    carol = users["carol"]
    out = await runtime.guard.call(carol, "reboot_host", {"host": "api-01"}, source="test")
    assert out.status == "pending_approval"
    with pytest.raises(PermissionError, match="own request"):
        await runtime.guard.approve(carol, out.approval_id)


async def test_concurrent_approvals_execute_once(tmp_path):
    raw_users = {
        "bob": {"role": "operator", "token": "demo-bob"},
        "carol": {"role": "admin", "token": "demo-carol"},
        "dana": {"role": "admin", "token": "demo-dana"},
    }
    config = demo_config(tmp_path, users=raw_users)
    rt = await build_runtime(config)
    try:
        out = await rt.guard.call(config.users["bob"], "reboot_host", {"host": "api-01"}, source="test")
        results = await asyncio.gather(
            rt.guard.approve(config.users["carol"], out.approval_id),
            rt.guard.approve(config.users["dana"], out.approval_id),
            return_exceptions=True,
        )
        assert sum(1 for r in results if isinstance(r, dict)) == 1
        assert sum(1 for r in results if isinstance(r, LookupError)) == 1
    finally:
        await rt.close()


async def test_rejected_request_never_runs(runtime, users):
    out = await runtime.guard.call(users["bob"], "drain_node", {"node": "gpu-01", "reason": "test"}, source="test")
    await runtime.guard.reject(users["carol"], out.approval_id)
    assert runtime.demo.slurm["nodes"]["gpu-01"]["state"] == "allocated"
    assert (await runtime.store.get_approval(out.approval_id))["status"] == "rejected"


async def test_secrets_in_tool_output_are_redacted(runtime, users):
    out = await runtime.guard.call(users["alice"], "container_logs", {"host": "api-01", "container": "api"}, source="test")
    text = "\n".join(out.result["lines"])
    assert "sk-live" not in text
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert "jane.doe@example.com" not in text
    assert "4111 1111 1111 1111" not in text
    assert out.redactions == 4


async def test_cannot_cancel_someone_elses_job(runtime, users):
    out = await runtime.guard.call(users["bob"], "cancel_job", {"job_id": "4522"}, source="test")  # alice's job
    assert out.status == "denied"
    assert runtime.demo.slurm["jobs"]["4522"]["state"] == "RUNNING"
    out = await runtime.guard.call(users["bob"], "cancel_job", {"job_id": "4521"}, source="test")  # bob's own
    assert out.status == "ok"


async def test_tool_rate_limit(tmp_path):
    config = demo_config(tmp_path, rate_limits={"tool_calls_per_minute": 3, "chat_messages_per_minute": 10})
    rt = await build_runtime(config)
    try:
        alice = config.users["alice"]
        statuses = [(await rt.guard.call(alice, "list_hosts", {}, source="test")).status for _ in range(5)]
        assert statuses == ["ok", "ok", "ok", "rate_limited", "rate_limited"]
        # Limits are per user.
        assert (await rt.guard.call(config.users["bob"], "list_hosts", {}, source="test")).status == "ok"
    finally:
        await rt.close()


async def test_rate_limit_holds_under_concurrency(tmp_path):
    config = demo_config(tmp_path, rate_limits={"tool_calls_per_minute": 10, "chat_messages_per_minute": 10})
    rt = await build_runtime(config)
    try:
        alice = config.users["alice"]
        outs = await asyncio.wait_for(
            asyncio.gather(*(rt.guard.call(alice, "list_hosts", {}, source="test") for _ in range(50))), timeout=20
        )
        assert sum(o.status == "ok" for o in outs) == 10
    finally:
        await rt.close()
