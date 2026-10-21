from __future__ import annotations

import pytest

from opsguard.config import ConfigError, parse_config
from opsguard.safety.policy import can_approve, decide
from tests.conftest import demo_config


def tool(runtime, name):
    return runtime.registry.get(name)


async def test_viewer_can_read_but_not_change(runtime, users):
    alice = users["alice"]
    assert decide(runtime.config, alice, tool(runtime, "host_overview"), {"host": "web-01"}).action == "allow"
    assert decide(runtime.config, alice, tool(runtime, "restart_container"), {"host": "web-01", "container": "nginx"}).action == "deny"


async def test_operator_changes_freely_but_dangerous_needs_approval(runtime, users):
    bob = users["bob"]
    assert decide(runtime.config, bob, tool(runtime, "restart_container"), {"host": "web-01", "container": "nginx"}).action == "allow"
    assert decide(runtime.config, bob, tool(runtime, "cleanup_logs"), {"host": "web-01", "path": "/var/log/app"}).action == "approval"


async def test_protected_target_escalates_change_to_approval(runtime, users):
    d = decide(runtime.config, users["bob"], tool(runtime, "restart_service"), {"host": "db-01", "service": "postgresql"})
    assert d.action == "approval"
    assert "protected" in d.reason


async def test_protected_target_never_loosens_a_deny(runtime, users):
    d = decide(runtime.config, users["alice"], tool(runtime, "restart_service"), {"host": "db-01", "service": "postgresql"})
    assert d.action == "deny"


async def test_reads_on_protected_targets_stay_allowed(runtime, users):
    assert decide(runtime.config, users["alice"], tool(runtime, "host_overview"), {"host": "db-01"}).action == "allow"


async def test_tool_override(tmp_path):
    from opsguard.runtime import build_runtime

    config = demo_config(tmp_path, tool_overrides={"restart_container": "deny"})
    rt = await build_runtime(config)
    try:
        d = decide(config, config.users["carol"], rt.registry.get("restart_container"), {"host": "web-01", "container": "nginx"})
        assert d.action == "deny"
    finally:
        await rt.close()


def test_approval_rules(config):
    assert can_approve(config, config.users["carol"], "bob")[0]
    assert not can_approve(config, config.users["carol"], "carol")[0]  # no self-approval
    assert not can_approve(config, config.users["bob"], "alice")[0]  # operators aren't approvers


@pytest.mark.parametrize("bad", [
    {"roles": {"viewer": {"read": "alow", "change": "deny", "dangerous": "deny"}}},
    {"users": {"x": {"role": "nobody", "token": "long-enough-token"}}},
    {"users": {"x": {"role": "viewer", "token": "short"}}},
    {"connectors": {"linux": "remote"}},
])
def test_config_typos_fail_loudly(bad):
    raw = {
        "roles": {"viewer": {"read": "allow", "change": "deny", "dangerous": "deny"}},
        "users": {"a": {"role": "viewer", "token": "token-aaaaaaaa"}},
    }
    raw.update(bad)
    with pytest.raises(ConfigError):
        parse_config(raw)
