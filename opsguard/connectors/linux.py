"""Linux host connector: health, services, logs, cleanup, reboot."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path
from typing import Any

from opsguard.connectors import _shell
from opsguard.connectors.demo_infra import DemoInfra
from opsguard.tools import Param, Tool, ToolContext, ToolError

HOST = Param("string", "Host name, e.g. web-01. Use list_hosts to see the options.")
LINES = Param("integer", "How many log lines to return (1-200, default 30).", required=False, default=30)

DESCRIPTIONS = {
    "list_hosts": "List the servers this assistant can reach.",
    "host_overview": "CPU, memory, load, disk usage and uptime for one server. Start here when something is slow or down.",
    "list_services": "System services on a server and whether each is running.",
    "service_logs": "Recent log lines for one system service (journald).",
    "restart_service": "Restart one system service. Routine change.",
    "cleanup_logs": "Delete old/rotated log files under a directory to free disk space. Destructive: deleted logs are gone.",
    "reboot_host": "Reboot a whole server. Destructive: everything on it goes down for a few minutes.",
}


def _lines(n: int) -> int:
    if not 1 <= n <= 200:
        raise ToolError("lines must be between 1 and 200")
    return n


def build(backend: str, demo: DemoInfra | None = None, cleanup_roots: list[str] | None = None) -> list[Tool]:
    impl = _Demo(demo) if backend == "demo" else _Local(cleanup_roots or [])

    def tool(name: str, risk: str, params: dict[str, Param]) -> Tool:
        return Tool(
            name=name, description=DESCRIPTIONS[name], risk=risk, params=params,
            handler=getattr(impl, name), connector="linux",
            target_params=("host",) if "host" in params else (),
        )

    return [
        tool("list_hosts", "read", {}),
        tool("host_overview", "read", {"host": HOST}),
        tool("list_services", "read", {"host": HOST}),
        tool("service_logs", "read", {"host": HOST, "service": Param("string", "Service name, e.g. nginx."), "lines": LINES}),
        tool("restart_service", "change", {"host": HOST, "service": Param("string", "Service name.")}),
        tool("cleanup_logs", "dangerous", {"host": HOST, "path": Param("string", "Log directory, e.g. /var/log/app.")}),
        tool("reboot_host", "dangerous", {"host": HOST}),
    ]


class _Demo:
    def __init__(self, infra: DemoInfra | None) -> None:
        assert infra is not None
        self.infra = infra

    def _host(self, host: str) -> dict[str, Any]:
        h = self.infra.hosts.get(host)
        if h is None:
            raise ToolError(f"unknown host {host!r}; known hosts: {', '.join(self.infra.hosts)}")
        return h

    def list_hosts(self, ctx: ToolContext) -> dict[str, Any]:
        return {"hosts": [{"host": name, "os": h["os"]} for name, h in self.infra.hosts.items()]}

    def host_overview(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        h = self._host(host)
        full = [d["mount"] for d in h["disks"] if d["used_percent"] >= 90]
        return {
            "host": host,
            "os": h["os"],
            "uptime_hours": h["uptime_hours"],
            "cpu_percent": h["cpu_percent"],
            "load_avg": h["load_avg"],
            "memory": h["memory"],
            "disks": h["disks"],
            "warnings": [f"disk {m} is almost full" for m in full],
        }

    def list_services(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        h = self._host(host)
        return {"host": host, "services": [{"name": n, "state": s} for n, s in h["services"].items()]}

    def service_logs(self, ctx: ToolContext, host: str, service: str, lines: int) -> dict[str, Any]:
        h = self._host(host)
        if service not in h["services"]:
            raise ToolError(f"no service {service!r} on {host}")
        logs = h["service_logs"].get(service, [f"{service}: no recent log entries"])
        return {"host": host, "service": service, "lines": logs[-_lines(lines):]}

    def restart_service(self, ctx: ToolContext, host: str, service: str) -> dict[str, Any]:
        h = self._host(host)
        if service not in h["services"]:
            raise ToolError(f"no service {service!r} on {host}")
        h["services"][service] = "running"
        return {"host": host, "service": service, "state": "running", "summary": f"restarted {service} on {host}"}

    def cleanup_logs(self, ctx: ToolContext, host: str, path: str) -> dict[str, Any]:
        h = self._host(host)
        if path not in h["log_dirs"]:
            raise ToolError(f"{path} is not a known log directory on {host}; known: {', '.join(h['log_dirs'])}")
        freed = h["log_dirs"][path]
        h["log_dirs"][path] = 0.2
        for disk in h["disks"]:
            if path.startswith(disk["mount"]) and disk["mount"] != "/":
                disk["used_percent"] = max(5.0, round(disk["used_percent"] - freed / disk["size_gb"] * 100, 1))
        return {"host": host, "path": path, "freed_gb": round(freed - 0.2, 1),
                "summary": f"deleted rotated logs in {path} on {host}, freed {freed - 0.2:.1f} GB"}

    def reboot_host(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        h = self._host(host)
        h["uptime_hours"] = 0
        return {"host": host, "summary": f"{host} rebooted"}


class _Local:
    """Talks to the machine OpsGuard runs on. Only the host name
    `localhost` (or this machine's hostname) is accepted."""

    def __init__(self, cleanup_roots: list[str]) -> None:
        self.cleanup_roots = [Path(p).resolve() for p in cleanup_roots]

    def _check(self, host: str) -> None:
        if host not in ("localhost", socket.gethostname()):
            raise ToolError("local mode only manages this machine; use host 'localhost'")

    def list_hosts(self, ctx: ToolContext) -> dict[str, Any]:
        return {"hosts": [{"host": "localhost", "hostname": socket.gethostname()}]}

    async def host_overview(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        self._check(host)
        # psutil blocks (cpu_percent samples for 0.3s), so keep it off the event loop.
        return await asyncio.to_thread(self._overview, host)

    def _overview(self, host: str) -> dict[str, Any]:
        import psutil

        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            disks.append({"mount": part.mountpoint, "size_gb": round(usage.total / 1e9, 1), "used_percent": usage.percent})
        mem = psutil.virtual_memory()
        load = list(os.getloadavg()) if hasattr(os, "getloadavg") else None
        return {
            "host": host,
            "hostname": socket.gethostname(),
            "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "load_avg": load,
            "memory": {"total_gb": round(mem.total / 1e9, 1), "used_percent": mem.percent},
            "disks": disks,
            "warnings": [f"disk {d['mount']} is almost full" for d in disks if d["used_percent"] >= 90],
        }

    async def list_services(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        self._check(host)
        out = await _shell.run(["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain", "--no-legend"])
        services = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 4:
                services.append({"name": parts[0].removesuffix(".service"), "state": parts[3]})
        return {"host": host, "services": services}

    async def service_logs(self, ctx: ToolContext, host: str, service: str, lines: int) -> dict[str, Any]:
        self._check(host)
        service = _shell.safe_name(service, "service")
        out = await _shell.run(["journalctl", "-u", service, "-n", str(_lines(lines)), "--no-pager", "-o", "short-iso"])
        return {"host": host, "service": service, "lines": out.splitlines()}

    async def restart_service(self, ctx: ToolContext, host: str, service: str) -> dict[str, Any]:
        self._check(host)
        service = _shell.safe_name(service, "service")
        await _shell.run(["systemctl", "restart", service], timeout=60)
        return {"host": host, "service": service, "summary": f"restarted {service}"}

    def cleanup_logs(self, ctx: ToolContext, host: str, path: str) -> dict[str, Any]:
        self._check(host)
        target = Path(path).resolve()
        if not any(target == root or root in target.parents for root in self.cleanup_roots):
            raise ToolError(f"{path} is outside the allowed cleanup_roots in opsguard.yaml")
        if not target.is_dir():
            raise ToolError(f"{path} is not a directory")
        # Only rotated/compressed logs, never the live log a process is writing to.
        freed = 0
        removed = 0
        for f in target.rglob("*"):
            if f.is_file() and not f.is_symlink() and (f.suffix in (".gz", ".xz", ".zst") or f.suffix.lstrip(".").isdigit()):
                freed += f.stat().st_size
                f.unlink()
                removed += 1
        return {"host": host, "path": str(target), "files_removed": removed, "freed_gb": round(freed / 1e9, 2),
                "summary": f"removed {removed} rotated log files, freed {freed / 1e9:.2f} GB"}

    def reboot_host(self, ctx: ToolContext, host: str) -> dict[str, Any]:
        raise ToolError("rebooting the machine OpsGuard runs on is disabled in local mode")
