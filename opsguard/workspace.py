"""Workspaces (tenants).

Every user belongs to one workspace. A workspace has its own approvals
queue, its own audit hash chain and, in demo mode, its own copy of the
simulated servers. Users in one workspace can't see or act on another
workspace's data. Roles and policy rules are shared by all workspaces.

The current workspace is carried in a ContextVar so code deep inside a
connector (the demo environment) can find the right tenant's state without
every function signature growing a `workspace` parameter. `Tool.run` sets it
from the calling user, so every tool execution is scoped automatically.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT = "default"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

current: ContextVar[str] = ContextVar("opsguard_workspace", default=DEFAULT)


@contextmanager
def use(workspace: str) -> Iterator[None]:
    token = current.set(workspace)
    try:
        yield
    finally:
        current.reset(token)
