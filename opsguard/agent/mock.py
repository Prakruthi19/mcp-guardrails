"""A scripted stand-in for the LLM.

Used when no ANTHROPIC_API_KEY is configured, so the public demo and the
test suite never depend on an API key, quota or network. It recognises the
kinds of requests in the demo ("why is web-01 down?", "restart nginx on
web-01", "why is job 4521 pending?") with simple patterns and plans tool
calls for them.

The important part is that it goes through exactly the same Guard as the
real model: it has no special access, so every block, approval and
redaction you see in mock mode is what the safety layer really does.
"""

from __future__ import annotations

import re
from typing import Any

from opsguard.agent.base import AgentReply, Step
from opsguard.config import User
from opsguard.guard import Guard, Outcome

_HOST = re.compile(r"\b(localhost|(?!cpu-|gpu-)[a-z]+-\d+)\b")
_NODE = re.compile(r"\b((?:cpu|gpu)-\d+)\b")
_JOB = re.compile(r"\bjob\s*#?\s*(\d+(?:_\d+)?)\b|\b(\d{4,})\b")
_APPROVAL = re.compile(r"\b(?:approval|request)\s*#?\s*(\d+)\b")
_PATH = re.compile(r"(/[\w./-]+)")
_SERVICE = re.compile(r"\bservice\s+([\w.@-]+)|\b([\w.@-]+)\s+service\b")
_CONTAINER = re.compile(r"\bcontainer\s+([\w.-]+)|\b([\w.-]+)\s+container\b")
KNOWN_CONTAINERS = ("nginx", "app", "api", "worker", "postgres", "redis")
KNOWN_SERVICES = ("sshd", "docker", "node_exporter", "postgresql", "cron")

HELP = (
    "I'm running in demo mode without an AI model, so I understand requests like:\n"
    "- Why is the website down on web-01?\n"
    "- Restart nginx on web-01\n"
    "- Clean up old logs on web-01\n"
    "- Show the api container logs on api-01\n"
    "- Why is job 4521 pending?\n"
    "- Cancel job 4523\n"
    "- Drain node gpu-01 for maintenance\n"
    "- What's the cluster status?\n"
    "Set ANTHROPIC_API_KEY to use Claude and ask anything."
)


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{w}", text) for w in words)


class MockAgent:
    name = "mock"

    def __init__(self, guard: Guard) -> None:
        self.guard = guard

    def reset(self, user: User, session_id: str) -> None:
        pass

    async def run(self, user: User, session_id: str, message: str, source: str = "web") -> AgentReply:
        text = message.lower()
        steps: list[Step] = []
        outcomes: list[Outcome] = []

        async def call(tool: str, **args: Any) -> Outcome:
            outcome = await self.guard.call(user, tool, args, source=source)
            steps.append(Step.from_outcome(outcome))
            outcomes.append(outcome)
            return outcome

        plan = self._intent(text, user)
        if plan is None:
            return AgentReply(HELP, steps)
        if plan[0] == "diagnose":
            await self._diagnose(plan[1], call)
            return AgentReply(self._diagnosis_text(outcomes), steps)
        tool, args = plan
        await call(tool, **args)
        return AgentReply("\n\n".join(describe(o) for o in outcomes), steps)

    def _intent(self, text: str, user: User) -> tuple[Any, ...] | None:
        host = _first(_HOST, text)
        node = _first(_NODE, text)
        job = _first(_JOB, text)
        approval = _first(_APPROVAL, text)
        service = _first(_SERVICE, text) or next((s for s in KNOWN_SERVICES if _has(text, s)), None)
        container = _first(_CONTAINER, text) or next((c for c in KNOWN_CONTAINERS if _has(text, c)), None)

        if approval and _has(text, "approval", "request"):
            return "approval_status", {"approval_id": int(approval)}
        if job and (_has(text, "job") or not host):
            if _has(text, "cancel", "kill", "stop"):
                return "cancel_job", {"job_id": job}
            return "job_status", {"job_id": job}
        if _has(text, "my jobs", "list jobs", "queue", "jobs"):
            return "list_jobs", {"user": user.slurm_user} if _has(text, "my") else {}
        if node and _has(text, "drain", "take .* out", "maintenance"):
            m = re.search(r"\b(?:because|for|reason:?)\s+(.+)$", text)
            return "drain_node", {"node": node, "reason": m.group(1).strip(" .") if m else "requested via OpsGuard"}
        if node and _has(text, "resume", "undrain", "return", "back"):
            return "resume_node", {"node": node}
        if _has(text, "cluster", "partition", "nodes", "slurm", "gpu"):
            return "cluster_status", {}
        if not host:
            if _has(text, "hosts", "servers", "machines"):
                return "list_hosts", {}
            return None
        if _has(text, "reboot"):
            return "reboot_host", {"host": host}
        if _has(text, "clean", "free", "delete old", "purge", "rotate"):
            path = _first(_PATH, text) or "/var/log/app"
            return "cleanup_logs", {"host": host, "path": path}
        if _has(text, "remove", "delete") and container:
            return "remove_container", {"host": host, "container": container}
        if _has(text, "restart"):
            if service and not _CONTAINER.search(text):
                return "restart_service", {"host": host, "service": service}
            if container:
                return "restart_container", {"host": host, "container": container}
        if _has(text, "log"):
            if service and not _CONTAINER.search(text):
                return "service_logs", {"host": host, "service": service}
            if container:
                return "container_logs", {"host": host, "container": container}
        if _has(text, "containers"):
            return "list_containers", {"host": host}
        if _has(text, "services"):
            return "list_services", {"host": host}
        return "diagnose", host

    async def _diagnose(self, host: str, call) -> None:
        overview = await call("host_overview", host=host)
        if overview.status != "ok":
            return
        containers = await call("list_containers", host=host)
        if containers.status != "ok":
            return
        broken = [c["name"] for c in containers.result["containers"] if c["status"] not in ("running", "Up")]
        for name in broken[:2]:
            await call("container_logs", host=host, container=name, lines=10)

    def _diagnosis_text(self, outcomes: list[Outcome]) -> str:
        parts = [describe(o) for o in outcomes]
        overview = outcomes[0]
        if overview.status != "ok":
            return "\n\n".join(parts)
        full_disks = [d for d in overview.result.get("disks", []) if d["used_percent"] >= 90]
        logs = " ".join(" ".join(o.result.get("lines", [])) for o in outcomes[2:] if o.status == "ok").lower()
        broken = [c["name"] for c in outcomes[1].result.get("containers", []) if c["status"] not in ("running", "Up")] \
            if len(outcomes) > 1 and outcomes[1].status == "ok" else []
        # The UI already shows every tool call, so the reply is just the conclusion.
        host = overview.args["host"]
        if full_disks and "no space left" in logs:
            d = full_disks[0]
            name = broken[0] if broken else "the service"
            return (f"**Cause:** {d['mount']} is {d['used_percent']:.0f}% full, so {name} can't write and crashes. "
                    f"A restart alone won't stick.\n\n**Fix:** clean up old logs on {host} (needs approval), then restart {name}.")
        if broken:
            return f"**Cause:** {', '.join(broken)} is not running. Check its logs."
        if full_disks:
            return f"{full_disks[0]['mount']} is almost full. Everything else looks fine."
        return f"Nothing looks wrong on {host} right now."


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    return next((g for g in m.groups() if g), None) if m.groups() else m.group(0)


def describe(o: Outcome) -> str:
    if o.status == "denied":
        return f"**Blocked.** {o.message.removeprefix('Not allowed: ')}"
    if o.status == "pending_approval":
        return f"**Waiting for approval** (request #{o.approval_id}). An admin has to approve it first."
    if o.status in ("error", "rate_limited"):
        return f"**Could not run** `{o.tool}`: {o.message}"
    r = o.result or {}
    fmt = _FORMATTERS.get(o.tool)
    text = fmt(r) if fmt else r.get("summary") or str(r)
    if o.redactions:
        text += f"\n\n_{o.redactions} secret(s) hidden._"
    return text


def _overview(r: dict[str, Any]) -> str:
    disks = ", ".join(
        f"{d['mount']} {d['used_percent']:.0f}%" + (" (almost full)" if d["used_percent"] >= 90 else "") for d in r["disks"]
    )
    load = f", load {r['load_avg'][0]}" if r.get("load_avg") else ""
    return (f"**{r['host']}**: CPU {r['cpu_percent']:.0f}%{load}, memory {r['memory']['used_percent']:.0f}% "
            f"of {r['memory']['total_gb']} GB, disks: {disks}, up {r['uptime_hours']} h.")


def _containers(r: dict[str, Any]) -> str:
    if not r["containers"]:
        return f"No containers on {r['host']}."
    items = ", ".join(
        f"{c['name']} ({c['status']}" + (f", exit code {c['exit_code']}" if c.get("exit_code") else "") + ")"
        for c in r["containers"]
    )
    return f"Containers on {r['host']}: {items}."


def _logs(r: dict[str, Any]) -> str:
    what = r.get("container") or r.get("service")
    body = "\n".join(r["lines"]) or "(no log lines)"
    return f"Recent logs from **{what}** on {r['host']}:\n```\n{body}\n```"


def _job(r: dict[str, Any]) -> str:
    text = f"Job **{r['job_id']}** ({r['name']}, user {r['user']}, partition {r['partition']}) is **{r['state']}**"
    if r.get("explanation"):
        text += f": {r['explanation']}"
    text += "."
    if r.get("related"):
        text += " Related: " + "; ".join(r["related"]) + "."
    return text


def _jobs(r: dict[str, Any]) -> str:
    if not r["jobs"]:
        return "No jobs found."
    return "Jobs:\n" + "\n".join(f"- {j['job_id']} {j['name']} ({j['user']}, {j['partition']}): {j['state']}" for j in r["jobs"])


def _cluster(r: dict[str, Any]) -> str:
    lines = [f"{r['jobs_running']} jobs running, {r['jobs_pending']} pending."]
    for p in r["partitions"]:
        nodes = ", ".join(f"{n['node']} {n['state']}" + (f" ({n['reason']})" if n["reason"] else "") for n in p["nodes"])
        lines.append(f"- **{p['partition']}**: {nodes}")
    return "\n".join(lines)


def _hosts(r: dict[str, Any]) -> str:
    return "Servers: " + ", ".join(h["host"] for h in r["hosts"]) + "."


def _services(r: dict[str, Any]) -> str:
    return f"Services on {r['host']}: " + ", ".join(f"{s['name']} ({s['state']})" for s in r["services"]) + "."


def _approval(r: dict[str, Any]) -> str:
    text = f"Request #{r['id']} (`{r['tool']}`) is **{r['status']}**"
    if r.get("decided_by"):
        text += f", decided by {r['decided_by']}"
    result = r.get("result") or {}
    inner = result.get("result") or {}
    if isinstance(inner, dict) and inner.get("summary"):
        text += f": {inner['summary']}"
    elif result.get("message"):
        text += f": {result['message']}"
    return text + "."


_FORMATTERS = {
    "host_overview": _overview,
    "list_containers": _containers,
    "container_logs": _logs,
    "service_logs": _logs,
    "job_status": _job,
    "list_jobs": _jobs,
    "cluster_status": _cluster,
    "list_hosts": _hosts,
    "list_services": _services,
    "approval_status": _approval,
}
