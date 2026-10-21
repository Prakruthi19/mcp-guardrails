"""Docker connector: containers, their logs, restart and remove."""

from __future__ import annotations

import json
from typing import Any

from opsguard.connectors import _shell
from opsguard.connectors.demo_infra import DemoInfra
from opsguard.connectors.linux import HOST, LINES, _lines
from opsguard.tools import Param, Tool, ToolContext, ToolError

CONTAINER = Param("string", "Container name, e.g. nginx. Use list_containers to see the options.")

DESCRIPTIONS = {
    "list_containers": "Docker containers on a server, with image, status and exit code.",
    "container_logs": "Recent log lines from one container. Use this to find out why a container stopped.",
    "restart_container": "Restart one container. Routine change; it will fail again if the underlying problem is not fixed.",
    "remove_container": "Delete a container. Destructive: its configuration and any unsaved data are lost.",
}


def build(backend: str, demo: DemoInfra | None = None) -> list[Tool]:
    impl = _Demo(demo) if backend == "demo" else _Local()

    def tool(name: str, risk: str, params: dict[str, Param]) -> Tool:
        return Tool(name=name, description=DESCRIPTIONS[name], risk=risk, params=params,
                    handler=getattr(impl, name), connector="docker", target_params=("host",))

    return [
        tool("list_containers", "read", {"host": HOST}),
        tool("container_logs", "read", {"host": HOST, "container": CONTAINER, "lines": LINES}),
        tool("restart_container", "change", {"host": HOST, "container": CONTAINER}),
        tool("remove_container", "dangerous", {"host": HOST, "container": CONTAINER}),
    ]


class _Demo:
    def __init__(self, infra: DemoInfra | None) -> None:
        assert infra is not None
        self.infra = infra

    def _container(self, host: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        h = self.infra.hosts.get(host)
        if h is None:
            raise ToolError(f"unknown host {host!r}; known hosts: {', '.join(self.infra.hosts)}")
        c = h["containers"].get(name)
        if c is None:
            known = ", ".join(h["containers"]) or "none"
            raise ToolError(f"no container {name!r} on {host}; containers there: {known}")
        return h, c

    def list_containers(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        h = self.infra.hosts.get(host)
        if h is None:
            raise ToolError(f"unknown host {host!r}; known hosts: {', '.join(self.infra.hosts)}")
        return {"host": host, "containers": [
            {"name": n, "image": c["image"], "status": c["status"], "exit_code": c["exit_code"]}
            for n, c in h["containers"].items()
        ]}

    def container_logs(self, ctx: ToolContext, host: str, container: str, lines: int) -> dict[str, Any]:
        _, c = self._container(host, container)
        return {"host": host, "container": container, "lines": c["logs"][-_lines(lines):]}

    def restart_container(self, ctx: ToolContext, host: str, container: str) -> dict[str, Any]:
        h, c = self._container(host, container)
        disk_full = any(d["used_percent"] >= 95 for d in h["disks"])
        if container == "nginx" and disk_full:
            # The real-world trap: restarting doesn't help while the disk is still full.
            c["status"], c["exit_code"] = "exited", 1
            c["logs"].append("[emerg] open() \"/var/cache/nginx/client_temp\" failed (28: No space left on device)")
            return {"host": host, "container": container, "status": "exited",
                    "summary": f"restarted {container} on {host} but it exited again: no space left on device"}
        c["status"], c["exit_code"] = "running", None
        c["logs"].append("[notice] start worker processes")
        return {"host": host, "container": container, "status": "running",
                "summary": f"restarted {container} on {host}; it is running"}

    def remove_container(self, ctx: ToolContext, host: str, container: str) -> dict[str, Any]:
        h, _ = self._container(host, container)
        del h["containers"][container]
        return {"host": host, "container": container, "summary": f"removed container {container} from {host}"}


class _Local:
    def _check(self, host: str) -> None:
        if host != "localhost":
            raise ToolError("local mode only manages Docker on this machine; use host 'localhost'")

    async def list_containers(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        self._check(host)
        out = await _shell.run(["docker", "ps", "-a", "--format", "{{json .}}"])
        containers = []
        for line in out.splitlines():
            c = json.loads(line)
            containers.append({"name": c.get("Names"), "image": c.get("Image"), "status": c.get("Status"), "state": c.get("State")})
        return {"host": host, "containers": containers}

    async def container_logs(self, ctx: ToolContext, host: str, container: str, lines: int) -> dict[str, Any]:
        self._check(host)
        name = _shell.safe_name(container, "container")
        # docker logs replays the container's stderr on its own stderr, and
        # error messages usually live there, so merge the two streams.
        out = await _shell.run(["docker", "logs", "--tail", str(_lines(lines)), name], merge_stderr=True)
        return {"host": host, "container": name, "lines": out.splitlines()}

    async def restart_container(self, ctx: ToolContext, host: str, container: str) -> dict[str, Any]:
        self._check(host)
        name = _shell.safe_name(container, "container")
        await _shell.run(["docker", "restart", name], timeout=60)
        state = (await _shell.run(["docker", "inspect", "-f", "{{.State.Status}}", name])).strip()
        return {"host": host, "container": name, "status": state, "summary": f"restarted {name}; status is {state}"}

    async def remove_container(self, ctx: ToolContext, host: str, container: str) -> dict[str, Any]:
        self._check(host)
        name = _shell.safe_name(container, "container")
        await _shell.run(["docker", "rm", "-f", name], timeout=60)
        return {"host": host, "container": name, "summary": f"removed container {name}"}
