"""Decides whether a tool call is allowed, needs approval, or is denied.

Pure function of (config, user, tool, args) - no I/O - so every rule is easy
to unit test and the same decision is made whether the call came from the
web UI, the MCP server or a test.

Order of evaluation:
  1. The user's role rule for the tool's risk level.
  2. A per-tool override in the config, if any, replaces it (lets an admin
     switch one tool off, or require approval for it).
  3. Protected targets: an allowed change/dangerous action that touches a
     protected host or node is escalated to approval.
Step 3 only ever tightens: a protected target can turn "allow" into
"approval", but nothing turns "deny" into anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opsguard.config import Config, User
from opsguard.tools import Tool

_STRICTNESS = {"allow": 0, "approval": 1, "deny": 2}


@dataclass(frozen=True)
class Decision:
    action: str  # allow | approval | deny
    reason: str


def _stricter(a: str, b: str) -> str:
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def decide(config: Config, user: User, tool: Tool, args: dict[str, Any]) -> Decision:
    role_rules = config.roles[user.role]
    action = role_rules[tool.risk]
    reason = f"role '{user.role}' has '{action}' for {tool.risk} actions"

    override = config.tool_overrides.get(tool.name)
    if override is not None:
        # An override can relax a role rule (e.g. allow one specific change
        # for viewers) or tighten it; it is the admin's explicit choice.
        action = override
        reason = f"tool override for '{tool.name}' is '{override}'"

    if tool.risk != "read" and action != "deny":
        hit = [t for t in tool.targets(args) if t in config.protected_targets]
        if hit:
            new_action = _stricter(action, "approval")
            if new_action != action:
                action = new_action
                reason = f"'{hit[0]}' is a protected target"

    return Decision(action=action, reason=reason)


def can_approve(config: Config, approver: User, requester_name: str) -> tuple[bool, str]:
    if approver.role not in config.approvers:
        return False, f"role '{approver.role}' cannot approve actions"
    if approver.name == requester_name:
        return False, "you cannot approve your own request"
    return True, "ok"
