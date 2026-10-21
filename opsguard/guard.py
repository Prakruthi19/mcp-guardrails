"""The Guard: the single path every tool call goes through.

    rate limit -> validate args -> policy -> (hold for approval | run) -> redact -> audit

The web chat, the MCP server and approved requests all call into here, so a
rule enforced in one place is enforced everywhere. The assistant (LLM) never
gets a handle on a tool function directly - only on `Guard.call`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from prometheus_client import Counter, Histogram

from opsguard.config import Config, User
from opsguard.safety.policy import can_approve, decide
from opsguard.safety.redact import redact
from opsguard.safety.store import Store
from opsguard.tools import Registry, ToolContext, ToolError, ToolPermissionError

logger = logging.getLogger("opsguard.guard")

TOOL_CALLS = Counter("opsguard_tool_calls_total", "Tool calls by outcome", ["tool", "status"])
TOOL_LATENCY = Histogram("opsguard_tool_seconds", "Tool execution time", ["tool"])
REDACTIONS = Counter("opsguard_redactions_total", "Secrets/PII values redacted from tool output")
APPROVALS = Counter("opsguard_approvals_total", "Approval decisions", ["decision"])


@dataclass
class Outcome:
    # ok | denied | pending_approval | rate_limited | error
    status: str
    tool: str
    args: dict[str, Any]
    result: Any = None
    message: str = ""
    approval_id: int | None = None
    redactions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Guard:
    def __init__(self, config: Config, registry: Registry, store: Store) -> None:
        self.config = config
        self.registry = registry
        self.store = store

    def _ctx(self, user: User) -> ToolContext:
        return ToolContext(user=user, is_admin=user.role == "admin")

    async def call(self, user: User, tool_name: str, args: dict[str, Any] | None, source: str) -> Outcome:
        args = args or {}
        audit = dict(actor=user.name, role=user.role, source=source, tool=tool_name, workspace=user.workspace)

        if not await self.store.hit_rate_limit(f"tools:{user.name}", self.config.tool_calls_per_minute):
            msg = f"rate limit: more than {self.config.tool_calls_per_minute} tool calls per minute"
            await self.store.audit(**audit, event="rate_limited", args=args, decision="deny", detail=msg)
            return self._count(Outcome("rate_limited", tool_name, args, message=msg))

        tool = self.registry.get(tool_name)
        if tool is None:
            msg = f"unknown tool {tool_name!r}"
            await self.store.audit(**audit, event="error", args=args, detail=msg)
            return self._count(Outcome("error", tool_name, args, message=msg))

        try:
            args = tool.validate(args)
        except ToolError as e:
            await self.store.audit(**audit, event="error", args=args, detail=str(e))
            return self._count(Outcome("error", tool_name, args, message=str(e)))

        decision = decide(self.config, user, tool, args)

        if decision.action == "deny":
            await self.store.audit(**audit, event="denied", args=args, decision="deny", detail=decision.reason)
            return self._count(Outcome("denied", tool_name, args, message=f"Not allowed: {decision.reason}."))

        if decision.action == "approval":
            approval_id = await self.store.create_approval(
                requester=user.name,
                requester_role=user.role,
                source=source,
                tool=tool_name,
                args=args,
                reason=decision.reason,
                workspace=user.workspace,
            )
            await self.store.audit(
                **audit, event="approval_requested", args=args, decision="approval",
                detail=decision.reason, approval_id=approval_id,
            )
            return self._count(Outcome(
                "pending_approval", tool_name, args, approval_id=approval_id,
                message=f"Held for approval (request #{approval_id}): {decision.reason}. "
                f"Someone with an approver role must approve it before it runs.",
            ))

        outcome = await self._execute(user, tool_name, args)
        await self.store.audit(
            **audit, event="executed" if outcome.status == "ok" else outcome.status, args=args,
            decision="allow", detail=_detail(outcome),
        )
        return self._count(outcome)

    async def _execute(self, user: User, tool_name: str, args: dict[str, Any]) -> Outcome:
        tool = self.registry.get(tool_name)
        assert tool is not None
        start = time.perf_counter()
        try:
            raw = await tool.run(self._ctx(user), args)
        except ToolPermissionError as e:
            return Outcome("denied", tool_name, args, message=f"Not allowed: {e}.")
        except ToolError as e:
            return Outcome("error", tool_name, args, message=str(e))
        except Exception:  # a bug in a connector must not take the server down or leak a traceback
            logger.exception("tool %s crashed", tool_name)
            return Outcome("error", tool_name, args, message="internal error while running the tool")
        finally:
            TOOL_LATENCY.labels(tool_name).observe(time.perf_counter() - start)
        result, n = redact(raw)
        REDACTIONS.inc(n)
        return Outcome("ok", tool_name, args, result=result, redactions=n)

    def _count(self, outcome: Outcome) -> Outcome:
        TOOL_CALLS.labels(outcome.tool, outcome.status).inc()
        return outcome

    # -- approvals -----------------------------------------------------------

    async def approve(self, approver: User, approval_id: int, source: str = "web") -> dict[str, Any]:
        return await self._decide_approval(approver, approval_id, approve=True, source=source)

    async def reject(self, approver: User, approval_id: int, source: str = "web") -> dict[str, Any]:
        return await self._decide_approval(approver, approval_id, approve=False, source=source)

    async def _decide_approval(self, approver: User, approval_id: int, *, approve: bool, source: str) -> dict[str, Any]:
        request = await self.store.get_approval(approval_id)
        # Another workspace's request looks exactly like a missing one, so
        # its existence isn't leaked across tenants.
        if request is None or request["workspace"] != approver.workspace:
            raise LookupError(f"no approval request #{approval_id}")
        ok, why = can_approve(self.config, approver, request["requester"])
        if not ok:
            raise PermissionError(why)
        requester = self.config.users.get(request["requester"])
        if requester is None:
            raise PermissionError(f"requester {request['requester']!r} no longer exists")

        if not await self.store.claim_approval(approval_id, approver.name, "approved" if approve else "rejected"):
            raise LookupError(f"request #{approval_id} was already decided")

        audit = dict(actor=approver.name, role=approver.role, source=source, tool=request["tool"],
                     args=request["args"], approval_id=approval_id, workspace=approver.workspace)
        if not approve:
            APPROVALS.labels("rejected").inc()
            await self.store.audit(**audit, event="approval_rejected", decision="deny",
                                   detail=f"rejected request from {requester.name}")
            return await self.store.get_approval(approval_id)

        APPROVALS.labels("approved").inc()
        await self.store.audit(**audit, event="approval_granted", decision="allow",
                               detail=f"approved request from {requester.name}")
        # Runs with the requester's identity: the approver said "yes, this may
        # happen", they did not take over the request.
        outcome = await self._execute(requester, request["tool"], request["args"])
        self._count(outcome)
        await self.store.finish_approval(
            approval_id, "executed" if outcome.status == "ok" else "failed",
            {"status": outcome.status, "result": outcome.result, "message": outcome.message},
        )
        await self.store.audit(
            actor=requester.name, role=requester.role, source=source, tool=request["tool"], args=request["args"],
            approval_id=approval_id, event="executed" if outcome.status == "ok" else outcome.status,
            decision="allow", detail=_detail(outcome), workspace=requester.workspace,
        )
        return await self.store.get_approval(approval_id)

    async def approval_status(self, user: User, approval_id: int) -> dict[str, Any]:
        request = await self.store.get_approval(approval_id)
        if request is None or request["workspace"] != user.workspace:
            raise ToolError(f"no approval request #{approval_id}")
        if request["requester"] != user.name and user.role not in self.config.approvers:
            raise ToolPermissionError("you can only check your own requests")
        return {k: request[k] for k in ("id", "tool", "args", "status", "decided_by", "result")}


def _detail(outcome: Outcome) -> str:
    """One line for the audit log. Only the action's own summary is kept,
    not the full output - logs and status dumps don't belong in an audit
    trail, even redacted."""
    if outcome.message:
        return outcome.message
    parts = []
    if isinstance(outcome.result, dict) and outcome.result.get("summary"):
        parts.append(str(outcome.result["summary"])[:200])
    if outcome.redactions:
        parts.append(f"{outcome.redactions} sensitive value(s) redacted from the output")
    return "; ".join(parts)
