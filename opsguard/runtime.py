"""Builds everything from a Config: connectors, the Guard, the store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opsguard.config import Config
from opsguard.connectors import docker, linux, slurm
from opsguard.connectors.demo_infra import DemoInfra
from opsguard.guard import Guard
from opsguard.safety.store import Store
from opsguard.tools import Param, Registry, Tool, ToolContext


@dataclass
class Runtime:
    config: Config
    registry: Registry
    store: Store
    guard: Guard
    demo: DemoInfra | None

    async def close(self) -> None:
        await self.store.close()


async def build_runtime(config: Config) -> Runtime:
    demo = DemoInfra() if config.demo_mode else None
    registry = Registry()
    backends = config.connectors
    if backends.get("linux", "off") != "off":
        for t in linux.build(backends["linux"], demo=demo, cleanup_roots=config.cleanup_roots):
            registry.add(t)
    if backends.get("docker", "off") != "off":
        for t in docker.build(backends["docker"], demo=demo):
            registry.add(t)
    if backends.get("slurm", "off") != "off":
        for t in slurm.build(backends["slurm"], demo=demo):
            registry.add(t)

    store = Store(config.database)
    await store.start()
    guard = Guard(config, registry, store)

    async def approval_status(ctx: ToolContext, approval_id: int) -> dict[str, Any]:
        return await guard.approval_status(ctx.user, approval_id)

    registry.add(Tool(
        name="approval_status",
        description="Check whether an action that was held for approval has been approved, rejected or run, and its result.",
        risk="read",
        params={"approval_id": Param("integer", "The approval request number.")},
        handler=approval_status,
        connector="opsguard",
    ))
    return Runtime(config=config, registry=registry, store=store, guard=guard, demo=demo)
