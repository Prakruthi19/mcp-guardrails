"""A snapshot of the infrastructure for the dashboard.

The dashboard is passive monitoring, like a Grafana page: it polls every few
seconds, so it isn't written to the audit log (that would bury real actions
in noise). It still respects policy - each section is built only from read
tools the viewing user is allowed to call without approval - and its output
is redacted like any tool result.

For every host, node and job it also says what the viewing user may do
there (allow / approval / deny), computed by the same `decide()` the Guard
uses, so the buttons in the UI show the real rules before anyone clicks.
"""

from __future__ import annotations

import logging
from typing import Any

from opsguard import workspace
from opsguard.config import User
from opsguard.runtime import Runtime
from opsguard.safety.policy import decide
from opsguard.safety.redact import redact
from opsguard.tools import ToolContext

logger = logging.getLogger("opsguard.overview")

HOST_ACTIONS = ("restart_service", "service_logs", "restart_container", "container_logs",
                "remove_container", "cleanup_logs", "reboot_host")
NODE_ACTIONS = ("drain_node", "resume_node")
JOB_ACTIONS = ("job_status", "cancel_job")


async def snapshot(rt: Runtime, user: User) -> dict[str, Any]:
    with workspace.use(user.workspace):
        return await _snapshot(rt, user)


async def _snapshot(rt: Runtime, user: User) -> dict[str, Any]:
    ctx = ToolContext(user=user, is_admin=user.role == "admin")

    def access(tool_name: str, args: dict[str, Any]) -> str | None:
        tool = rt.registry.get(tool_name)
        return decide(rt.config, user, tool, args).action if tool else None

    async def read(tool_name: str, **args: Any) -> Any:
        """Run a read tool directly, if this user may; None if not or if it fails."""
        if access(tool_name, args) != "allow":
            return None
        tool = rt.registry.get(tool_name)
        try:
            result, _ = redact(await tool.run(ctx, tool.validate(args)))
            return result
        except Exception as e:  # a missing docker/slurm CLI shouldn't break the page
            logger.debug("overview: %s failed: %s", tool_name, e)
            return None

    hosts = []
    listed = await read("list_hosts")
    for entry in (listed or {}).get("hosts", []):
        name = entry["host"]
        overview = await read("host_overview", host=name) or {}
        services = (await read("list_services", host=name) or {}).get("services")
        containers = (await read("list_containers", host=name) or {}).get("containers")
        hosts.append({
            "host": name,
            "os": overview.get("os") or entry.get("os") or entry.get("hostname"),
            "uptime_hours": overview.get("uptime_hours"),
            "cpu_percent": overview.get("cpu_percent"),
            "memory": overview.get("memory"),
            "disks": overview.get("disks", []),
            "services": services,
            "containers": [_container(c) for c in containers] if containers is not None else None,
            "log_dirs": _log_dirs(rt, name),
            "protected": name in rt.config.protected_targets,
            "access": {a: access(a, {"host": name}) for a in HOST_ACTIONS},
        })

    slurm = None
    status = await read("cluster_status")
    if status is not None:
        jobs = (await read("list_jobs") or {}).get("jobs", [])
        nodes = _nodes(status)
        slurm = {
            "nodes": [{**n, "access": {a: access(a, {"node": n["node"], "reason": "x"}) for a in NODE_ACTIONS},
                       "protected": n["node"] in rt.config.protected_targets} for n in nodes],
            "jobs": [{**j, "access": {a: access(a, {"job_id": j["job_id"]}) for a in JOB_ACTIONS}} for j in jobs],
        }

    return {"hosts": hosts, "slurm": slurm, "incidents": _incidents(hosts, slurm)}


def _container(c: dict[str, Any]) -> dict[str, Any]:
    # Demo containers have a plain status; `docker ps` gives State plus a
    # human Status like "Exited (1) 2 hours ago".
    state = c.get("state") or c.get("status") or "unknown"
    return {"name": c.get("name"), "image": c.get("image"), "state": state.lower(),
            "detail": c.get("status") if c.get("state") else None, "exit_code": c.get("exit_code")}


def _log_dirs(rt: Runtime, host: str) -> list[dict[str, Any]]:
    if rt.demo is not None and host in rt.demo.hosts:
        return [{"path": p, "size_gb": gb} for p, gb in rt.demo.hosts[host]["log_dirs"].items()]
    return [{"path": p, "size_gb": None} for p in rt.config.cleanup_roots]


def _nodes(status: dict[str, Any]) -> list[dict[str, Any]]:
    if "partitions" in status:  # demo shape: nodes grouped by partition
        return [{"partition": p["partition"], **n} for p in status["partitions"] for n in p["nodes"]]
    return status.get("nodes", [])


def _incidents(hosts: list[dict[str, Any]], slurm: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for h in hosts:
        for c in h["containers"] or []:
            if c["state"] in ("exited", "dead", "restarting"):
                out.append({"severity": "critical", "target": h["host"],
                            "title": f"{c['name']} container is {c['state']} on {h['host']}"})
        for s in h["services"] or []:
            if s["state"] in ("failed", "exited", "stopped"):
                out.append({"severity": "critical", "target": h["host"],
                            "title": f"{s['name']} service is {s['state']} on {h['host']}"})
        for d in h["disks"]:
            if d["used_percent"] >= 90:
                out.append({"severity": "warning", "target": h["host"],
                            "title": f"{d['mount']} is {d['used_percent']:.0f}% full on {h['host']}"})
    if slurm:
        for n in slurm["nodes"]:
            if str(n["state"]).lower().startswith(("drain", "down")):
                reason = f": {n['reason']}" if n.get("reason") else ""
                out.append({"severity": "warning", "target": n["node"], "title": f"{n['node']} is {n['state']}{reason}"})
        for j in slurm["jobs"]:
            if j["state"] == "FAILED":
                out.append({"severity": "warning", "target": j["job_id"], "title": f"job {j['job_id']} ({j['name']}) failed"})
    return out
