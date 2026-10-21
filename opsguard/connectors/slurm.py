"""Slurm connector: cluster state, jobs, and node maintenance.

The questions HPC admins answer all day - "why is my job pending?",
"why did it fail?" - are answered from scheduler data, and the reason
codes Slurm uses are translated into plain language.

Ownership rule: anyone allowed to cancel jobs may only cancel their *own*
jobs, unless they are an admin. This is a connector-specific rule (the
Guard's role policy doesn't know who owns a job), enforced here by raising
ToolPermissionError, which the Guard records as a denial.
"""

from __future__ import annotations

import re
from typing import Any

from opsguard.connectors import _shell
from opsguard.connectors.demo_infra import DemoInfra
from opsguard.tools import Param, Tool, ToolContext, ToolError, ToolPermissionError

JOB_ID = Param("string", "Slurm job id, e.g. 4521.")
NODE = Param("string", "Node name, e.g. gpu-02.")

DESCRIPTIONS = {
    "cluster_status": "Slurm partitions and node states (idle, allocated, drained) with drain reasons.",
    "list_jobs": "Slurm jobs in the queue, optionally only one user's jobs.",
    "job_status": "Details for one Slurm job, including a plain-language explanation of why it is pending or why it failed.",
    "cancel_job": "Cancel a Slurm job. Users can only cancel their own jobs; admins can cancel any.",
    "drain_node": "Take a node out of service (running jobs finish, no new jobs start). Affects every user of the cluster.",
    "resume_node": "Return a drained node to service.",
}

# What Slurm's pending reason codes actually mean, for the person waiting.
PENDING_REASONS = {
    "Resources": "the resources it asked for are all in use or unavailable; it will start when enough free up",
    "Priority": "other jobs with higher priority are queued ahead of it",
    "ReqNodeNotAvail": "a node it requires is down, drained or reserved",
    "QOSMaxGRESPerUser": "you have reached your quality-of-service limit for GPUs",
    "AssocGrpCPULimit": "your account/group has reached its CPU limit",
    "Dependency": "it is waiting for another job it depends on to finish",
    "JobHeldUser": "it was put on hold by its owner (scontrol release to start it)",
    "JobHeldAdmin": "it was put on hold by an administrator",
    "BeginTime": "it was submitted with a start time that has not arrived yet",
}

_JOB_ID_RE = re.compile(r"^\d+(_\d+)?$")


def _job_id(value: str) -> str:
    if not _JOB_ID_RE.match(value):
        raise ToolError(f"invalid job id {value!r}")
    return value


def build(backend: str, demo: DemoInfra | None = None) -> list[Tool]:
    impl = _Demo(demo) if backend == "demo" else _Local()

    def tool(name: str, risk: str, params: dict[str, Param], targets: tuple[str, ...] = ()) -> Tool:
        return Tool(name=name, description=DESCRIPTIONS[name], risk=risk, params=params,
                    handler=getattr(impl, name), connector="slurm", target_params=targets)

    return [
        tool("cluster_status", "read", {}),
        tool("list_jobs", "read", {"user": Param("string", "Only show this user's jobs.", required=False)}),
        tool("job_status", "read", {"job_id": JOB_ID}),
        tool("cancel_job", "change", {"job_id": JOB_ID}),
        tool("drain_node", "dangerous", {"node": NODE, "reason": Param("string", "Why the node is being drained.")}, ("node",)),
        tool("resume_node", "change", {"node": NODE}, ("node",)),
    ]


class _Demo:
    def __init__(self, infra: DemoInfra | None) -> None:
        assert infra is not None
        self.infra = infra

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.infra.slurm["jobs"].get(_job_id(job_id))
        if job is None:
            raise ToolError(f"no job {job_id}")
        return job

    def _node(self, node: str) -> dict[str, Any]:
        n = self.infra.slurm["nodes"].get(node)
        if n is None:
            raise ToolError(f"unknown node {node!r}; nodes: {', '.join(self.infra.slurm['nodes'])}")
        return n

    def cluster_status(self, ctx: ToolContext) -> dict[str, Any]:
        s = self.infra.slurm
        partitions = []
        for name, p in s["partitions"].items():
            nodes = [{"node": n, **{k: s["nodes"][n][k] for k in ("state", "cpus", "gpus", "reason")}} for n in p["nodes"]]
            partitions.append({"partition": name, "max_time": p["max_time"], "nodes": nodes})
        queued = sum(1 for j in s["jobs"].values() if j["state"] == "PENDING")
        running = sum(1 for j in s["jobs"].values() if j["state"] == "RUNNING")
        return {"partitions": partitions, "jobs_running": running, "jobs_pending": queued}

    def list_jobs(self, ctx: ToolContext, user: str | None) -> dict[str, Any]:
        jobs = [
            {"job_id": jid, "user": j["user"], "name": j["name"], "partition": j["partition"], "state": j["state"]}
            for jid, j in self.infra.slurm["jobs"].items()
            if user is None or j["user"] == user
        ]
        return {"jobs": jobs}

    def job_status(self, ctx: ToolContext, job_id: str) -> dict[str, Any]:
        j = self._job(job_id)
        out = {"job_id": job_id, **{k: j[k] for k in ("user", "name", "partition", "state", "reason", "gpus", "time_used", "node")}}
        if j["state"] == "PENDING":
            explanation = PENDING_REASONS.get(j["reason"], f"Slurm reason code: {j['reason']}")
            partition = self.infra.slurm["partitions"][j["partition"]]
            unavailable = [
                f"{n} is {self.infra.slurm['nodes'][n]['state']} ({self.infra.slurm['nodes'][n]['reason']})"
                for n in partition["nodes"] if self.infra.slurm["nodes"][n]["state"] in ("drained", "down")
            ]
            out["explanation"] = explanation
            if unavailable:
                out["related"] = unavailable
        elif j["state"] == "FAILED":
            out["explanation"] = f"the job failed: {j['reason']}"
        return out

    def cancel_job(self, ctx: ToolContext, job_id: str) -> dict[str, Any]:
        j = self._job(job_id)
        if not ctx.is_admin and j["user"] != ctx.user.slurm_user:
            raise ToolPermissionError(f"job {job_id} belongs to {j['user']}; you can only cancel your own jobs")
        if j["state"] not in ("PENDING", "RUNNING"):
            raise ToolError(f"job {job_id} is already {j['state']}")
        j["state"], j["reason"] = "CANCELLED", f"cancelled by {ctx.user.name}"
        return {"job_id": job_id, "state": "CANCELLED", "summary": f"cancelled job {job_id} ({j['name']})"}

    def drain_node(self, ctx: ToolContext, node: str, reason: str) -> dict[str, Any]:
        n = self._node(node)
        n["state"], n["reason"] = "drained", reason
        return {"node": node, "state": "drained", "summary": f"drained {node}: {reason}"}

    def resume_node(self, ctx: ToolContext, node: str) -> dict[str, Any]:
        n = self._node(node)
        n["state"], n["reason"] = "idle", None
        # A freed GPU node lets a pending GPU job start, as it would for real.
        for jid, j in self.infra.slurm["jobs"].items():
            partition = self.infra.slurm["partitions"][j["partition"]]
            if j["state"] == "PENDING" and node in partition["nodes"]:
                j.update(state="RUNNING", reason=None, node=node, time_used="0:00:05")
                n["state"] = "allocated"
                break
        return {"node": node, "state": n["state"], "summary": f"resumed {node}; state is now {n['state']}"}


class _Local:
    """Uses the Slurm command-line tools on this machine (squeue, sinfo,
    scontrol, scancel). Needs to run on a login/admin node of the cluster."""

    async def cluster_status(self, ctx: ToolContext) -> dict[str, Any]:
        out = await _shell.run(["sinfo", "-h", "-N", "-o", "%N|%P|%T|%c|%G|%E"])
        nodes = []
        for line in out.splitlines():
            name, partition, state, cpus, gres, reason = (line.split("|") + [""] * 6)[:6]
            nodes.append({"node": name, "partition": partition.rstrip("*"), "state": state, "cpus": cpus,
                          "gres": gres, "reason": None if reason in ("", "none") else reason})
        return {"nodes": nodes}

    async def list_jobs(self, ctx: ToolContext, user: str | None) -> dict[str, Any]:
        argv = ["squeue", "-h", "-o", "%i|%u|%j|%P|%T|%r"]
        if user:
            argv += ["-u", _shell.safe_name(user, "user")]
        out = await _shell.run(argv)
        jobs = []
        for line in out.splitlines():
            jid, u, name, partition, state, reason = (line.split("|") + [""] * 6)[:6]
            jobs.append({"job_id": jid, "user": u, "name": name, "partition": partition, "state": state, "reason": reason})
        return {"jobs": jobs}

    async def _fields(self, job_id: str) -> dict[str, str]:
        out = await _shell.run(["scontrol", "show", "job", _job_id(job_id), "-o"])
        return dict(item.split("=", 1) for item in out.split() if "=" in item)

    async def job_status(self, ctx: ToolContext, job_id: str) -> dict[str, Any]:
        f = await self._fields(job_id)
        reason = f.get("Reason", "None")
        out = {"job_id": job_id, "user": f.get("UserId", "").split("(")[0], "name": f.get("JobName"),
               "partition": f.get("Partition"), "state": f.get("JobState"), "reason": reason,
               "time_used": f.get("RunTime"), "node": f.get("NodeList"), "tres": f.get("TRES")}
        if f.get("JobState") == "PENDING":
            out["explanation"] = PENDING_REASONS.get(reason, f"Slurm reason code: {reason}")
        return out

    async def cancel_job(self, ctx: ToolContext, job_id: str) -> dict[str, Any]:
        f = await self._fields(job_id)
        owner = f.get("UserId", "").split("(")[0]
        if not ctx.is_admin and owner != ctx.user.slurm_user:
            raise ToolPermissionError(f"job {job_id} belongs to {owner}; you can only cancel your own jobs")
        await _shell.run(["scancel", _job_id(job_id)])
        return {"job_id": job_id, "summary": f"cancelled job {job_id}"}

    async def drain_node(self, ctx: ToolContext, node: str, reason: str) -> dict[str, Any]:
        node = _shell.safe_name(node, "node")
        await _shell.run(["scontrol", "update", f"NodeName={node}", "State=DRAIN", f"Reason={reason[:200]}"])
        return {"node": node, "summary": f"drained {node}: {reason}"}

    async def resume_node(self, ctx: ToolContext, node: str) -> dict[str, Any]:
        node = _shell.safe_name(node, "node")
        await _shell.run(["scontrol", "update", f"NodeName={node}", "State=RESUME"])
        return {"node": node, "summary": f"resumed {node}"}
