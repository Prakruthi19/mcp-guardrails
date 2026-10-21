"""A small simulated environment for the public demo.

Three servers and a Slurm cluster with problems worth asking about:
  - web-01: the website is down. nginx exited because /var is 97% full of
    old app logs. Restarting nginx alone fails again until the logs are
    cleaned up - so the "obvious" fix needs a dangerous action, which
    needs approval.
  - api-01: healthy, but its logs contain a leaked secret and a customer
    email, to show redaction.
  - db-01: the production database. Marked protected in opsguard.yaml.
  - Slurm: a GPU job stuck pending because gpu-02 is drained.

State is in memory and mutable, so actions have visible effects. `reset()`
puts everything back for the next visitor. Each workspace (tenant) gets its
own copy, created on first use, so one company's fixes never show up in
another's servers.
"""

from __future__ import annotations

import copy
from typing import Any

from opsguard import workspace

_INITIAL: dict[str, Any] = {
    "hosts": {
        "web-01": {
            "os": "Ubuntu 24.04",
            "uptime_hours": 412,
            "cpu_percent": 6.0,
            "load_avg": [0.21, 0.30, 0.28],
            "memory": {"total_gb": 8, "used_percent": 41.0},
            "disks": [
                {"mount": "/", "size_gb": 40, "used_percent": 38.0},
                {"mount": "/var", "size_gb": 50, "used_percent": 97.0},
            ],
            "log_dirs": {"/var/log/app": 23.4},
            "services": {
                "sshd": "running",
                "docker": "running",
                "node_exporter": "running",
            },
            "service_logs": {
                "docker": [
                    "dockerd: container nginx exited with code 1",
                    "dockerd: restart policy 'no' - not restarting nginx",
                ],
            },
            "containers": {
                "nginx": {
                    "image": "nginx:1.27",
                    "status": "exited",
                    "exit_code": 1,
                    "logs": [
                        "2026/10/20 03:12:44 [notice] start worker processes",
                        "2026/10/20 03:14:02 [alert] write() to \"/var/log/nginx/access.log\" failed (28: No space left on device)",
                        "2026/10/20 03:14:02 [emerg] open() \"/var/cache/nginx/client_temp\" failed (28: No space left on device)",
                        "2026/10/20 03:14:02 [notice] exiting",
                    ],
                },
                "app": {
                    "image": "shop-app:2.3.1",
                    "status": "running",
                    "exit_code": None,
                    "logs": [
                        "INFO request GET /api/cart 200 12ms",
                        "WARN log file /var/log/app/app.log is 23.1 GB, rotation disabled",
                    ],
                },
            },
        },
        "api-01": {
            "os": "Ubuntu 24.04",
            "uptime_hours": 1290,
            "cpu_percent": 22.0,
            "load_avg": [0.92, 0.85, 0.80],
            "memory": {"total_gb": 16, "used_percent": 63.0},
            "disks": [{"mount": "/", "size_gb": 80, "used_percent": 54.0}],
            "log_dirs": {"/var/log/app": 2.1},
            "services": {"sshd": "running", "docker": "running"},
            "service_logs": {},
            "containers": {
                "api": {
                    "image": "billing-api:5.0.2",
                    "status": "running",
                    "exit_code": None,
                    "logs": [
                        "INFO starting billing-api 5.0.2",
                        "DEBUG config loaded: STRIPE_API_KEY=sk-live-4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c",
                        "DEBUG aws credentials AKIAIOSFODNN7EXAMPLE loaded from env",
                        "WARN payment retry for customer jane.doe@example.com card 4111 1111 1111 1111",
                        "INFO request POST /v1/charge 201 88ms",
                    ],
                },
                "worker": {
                    "image": "billing-worker:5.0.2",
                    "status": "running",
                    "exit_code": None,
                    "logs": ["INFO processed 1,204 jobs in the last hour"],
                },
            },
        },
        "db-01": {
            "os": "Debian 12",
            "uptime_hours": 2210,
            "cpu_percent": 31.0,
            "load_avg": [1.4, 1.3, 1.2],
            "memory": {"total_gb": 64, "used_percent": 78.0},
            "disks": [
                {"mount": "/", "size_gb": 40, "used_percent": 29.0},
                {"mount": "/var/lib/postgresql", "size_gb": 500, "used_percent": 71.0},
            ],
            "log_dirs": {"/var/log/postgresql": 4.8},
            "services": {"sshd": "running", "postgresql": "running"},
            "service_logs": {
                "postgresql": [
                    "LOG:  checkpoint complete: wrote 18234 buffers (13.9%)",
                    "LOG:  automatic vacuum of table \"shop.public.orders\"",
                ],
            },
            "containers": {},
        },
    },
    "slurm": {
        "partitions": {
            "cpu": {"nodes": ["cpu-01", "cpu-02", "cpu-03", "cpu-04"], "max_time": "2-00:00:00"},
            "gpu": {"nodes": ["gpu-01", "gpu-02"], "max_time": "1-00:00:00"},
        },
        "nodes": {
            "cpu-01": {"state": "allocated", "cpus": 64, "gpus": 0, "reason": None},
            "cpu-02": {"state": "allocated", "cpus": 64, "gpus": 0, "reason": None},
            "cpu-03": {"state": "idle", "cpus": 64, "gpus": 0, "reason": None},
            "cpu-04": {"state": "idle", "cpus": 64, "gpus": 0, "reason": None},
            "gpu-01": {"state": "allocated", "cpus": 32, "gpus": 4, "reason": None},
            "gpu-02": {"state": "drained", "cpus": 32, "gpus": 4, "reason": "fan failure - ticket HW-118"},
        },
        "jobs": {
            "4521": {"user": "bob", "name": "train-resnet", "partition": "gpu", "state": "PENDING",
                     "reason": "Resources", "gpus": 4, "time_used": "0:00", "node": None},
            "4522": {"user": "alice", "name": "rna-align", "partition": "cpu", "state": "RUNNING",
                     "reason": None, "gpus": 0, "time_used": "3:12:40", "node": "cpu-01"},
            "4523": {"user": "bob", "name": "etl-nightly", "partition": "cpu", "state": "FAILED",
                     "reason": "OutOfMemory (used 64 GB of 64 GB requested)", "gpus": 0,
                     "time_used": "0:41:09", "node": "cpu-02"},
            "4524": {"user": "carol", "name": "llm-finetune", "partition": "gpu", "state": "RUNNING",
                     "reason": None, "gpus": 4, "time_used": "11:02:17", "node": "gpu-01"},
        },
    },
}


class DemoInfra:
    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}

    @property
    def state(self) -> dict[str, Any]:
        ws = workspace.current.get()
        if ws not in self._states:
            self._states[ws] = copy.deepcopy(_INITIAL)
        return self._states[ws]

    def reset(self) -> None:
        """Reset the current workspace's servers."""
        self._states.pop(workspace.current.get(), None)

    @property
    def hosts(self) -> dict[str, Any]:
        return self.state["hosts"]

    @property
    def slurm(self) -> dict[str, Any]:
        return self.state["slurm"]
