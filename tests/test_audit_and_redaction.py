from __future__ import annotations

import pytest

from opsguard.safety.redact import redact, redact_text
from opsguard.safety.suggest import suggest_policy


async def test_audit_chain_detects_tampering(runtime, users):
    for host in ("web-01", "api-01", "db-01"):
        await runtime.guard.call(users["alice"], "host_overview", {"host": host}, source="test")
    assert (await runtime.store.verify_audit()) == {"ok": True, "checked": 3, "broken_at": None}

    # Someone quietly rewrites history to hide who did something.
    await runtime.store.db.execute("UPDATE audit SET actor = 'mallory' WHERE id = 2")
    result = await runtime.store.verify_audit()
    assert result["ok"] is False
    assert result["broken_at"] == 2


async def test_audit_chain_detects_deleted_entries(runtime, users):
    for host in ("web-01", "api-01", "db-01"):
        await runtime.guard.call(users["alice"], "host_overview", {"host": host}, source="test")
    await runtime.store.db.execute("DELETE FROM audit WHERE id = 2")
    result = await runtime.store.verify_audit()
    assert result == {"ok": False, "checked": 1, "broken_at": 3}


@pytest.mark.parametrize("text", [
    "DB_PASSWORD=hunter2hunter2",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "key AKIAIOSFODNN7EXAMPLE",
    "token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "contact ops@example.org",
    "ssn 123-45-6789",
    "card 4111-1111-1111-1111",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
])
def test_redacts_secrets(text):
    out, n = redact_text(text)
    assert n >= 1
    assert "[REDACTED]" in out


def test_leaves_ordinary_text_alone():
    text = "order 1234 5678 9012 3456 shipped; job 4521 pending; disk /var 97% full"
    out, n = redact_text(text)
    assert out == text  # card-shaped but fails Luhn, so it's not a card
    assert n == 0


def test_redact_walks_nested_values():
    value, n = redact({"lines": ["password: abcdefgh", "fine"], "meta": {"email": "a@b.co"}})
    assert value == {"lines": ["password: [REDACTED]", "fine"], "meta": {"email": "[REDACTED]"}}
    assert n == 2


async def test_suggest_policy_flags_unused_tools(runtime, users):
    await runtime.guard.call(users["bob"], "host_overview", {"host": "web-01"}, source="test")
    await runtime.guard.call(users["bob"], "restart_container", {"host": "web-01", "container": "app"}, source="test")
    entries = await runtime.store.audit_entries(0, 1000)
    report = suggest_policy(runtime.config, runtime.registry, entries)
    assert report["roles"]["operator"]["used"] == {"host_overview": 1, "restart_container": 1}
    suggested = report["suggested_tool_overrides"]
    assert "restart_container" not in suggested  # used
    assert suggested.get("reboot_host") == "deny"  # never used
    assert "host_overview" not in suggested  # reads are never suggested
