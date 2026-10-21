"""Least-privilege suggestions from real usage ("learn, then lock").

Agents and teams are usually given far more tools than they use. Run
OpsGuard for a while, then this reads the audit log and reports, per role,
which tools were actually used and which were allowed but never touched.
Tools nobody used are suggested as `deny` overrides: if a need shows up
later, it fails loudly and someone turns it back on deliberately, instead
of an unused capability sitting there waiting to be misused.

This only suggests; it never changes the config by itself.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from opsguard.config import Config
from opsguard.tools import Registry

_USED_EVENTS = ("executed", "approval_requested", "denied")


def suggest_policy(config: Config, registry: Registry, entries: list[dict[str, Any]]) -> dict[str, Any]:
    used: dict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        if e["event"] in _USED_EVENTS and e["tool"]:
            used[e["role"]][e["tool"]] += 1

    roles = {}
    for role, rules in config.roles.items():
        allowed = [t.name for t in registry.all() if rules[t.risk] != "deny"]
        roles[role] = {
            "used": dict(used[role].most_common()),
            "allowed_but_unused": sorted(t for t in allowed if t not in used[role]),
        }

    everyone = set().union(*(set(c) for c in used.values())) if used else set()
    never_used = sorted(
        t.name for t in registry.all()
        if t.risk != "read" and t.name not in everyone and t.name not in config.tool_overrides
    )
    return {
        "observed_calls": sum(sum(c.values()) for c in used.values()),
        "roles": roles,
        "suggested_tool_overrides": {name: "deny" for name in never_used},
    }
