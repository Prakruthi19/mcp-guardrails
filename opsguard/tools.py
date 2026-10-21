"""Tool definitions shared by every connector.

A connector only describes its tools (name, description, parameters, risk
level) and implements them. Everything safety-related - who may call what,
approvals, rate limits, redaction, audit - lives in the Guard and applies to
every tool the same way, so a new connector gets all of it for free.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from opsguard import workspace
from opsguard.config import RISK_LEVELS, User


class ToolError(Exception):
    """The tool ran (or tried to) and failed: unknown host, command error, etc."""


class ToolPermissionError(Exception):
    """A connector-specific rule refused the call, e.g. cancelling someone else's job."""


@dataclass(frozen=True)
class ToolContext:
    user: User
    is_admin: bool


Handler = Callable[..., Any | Awaitable[Any]]


@dataclass(frozen=True)
class Param:
    type: str  # JSON schema type: string, integer
    description: str
    required: bool = True
    default: Any = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    risk: str
    params: dict[str, Param]
    handler: Handler
    connector: str
    # Argument names whose values identify the thing being acted on (a host,
    # a node). Checked against `protected_targets` in the config.
    target_params: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"tool {self.name}: unknown risk {self.risk!r}")

    def input_schema(self) -> dict[str, Any]:
        properties = {
            name: {"type": p.type, "description": p.description} for name, p in self.params.items()
        }
        required = [name for name, p in self.params.items() if p.required]
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return args with defaults filled in, or raise ToolError.

        Arguments come from an LLM, so they are checked strictly: unknown
        keys, missing required keys and wrong types are all rejected rather
        than coerced.
        """
        if not isinstance(args, dict):
            raise ToolError("arguments must be an object")
        unknown = set(args) - set(self.params)
        if unknown:
            raise ToolError(f"unknown argument(s): {', '.join(sorted(unknown))}")
        clean: dict[str, Any] = {}
        for name, p in self.params.items():
            if name not in args or args[name] is None:
                if p.required:
                    raise ToolError(f"missing required argument: {name}")
                clean[name] = p.default
                continue
            value = args[name]
            if p.type == "integer":
                # bool is an int subclass in Python; "true" is not a line count.
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ToolError(f"argument {name} must be an integer")
            elif p.type == "string":
                if not isinstance(value, str) or not value.strip():
                    raise ToolError(f"argument {name} must be a non-empty string")
                value = value.strip()
            clean[name] = value
        return clean

    def targets(self, args: dict[str, Any]) -> list[str]:
        return [str(args[k]) for k in self.target_params if args.get(k)]

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        # Scope the call to the caller's workspace (see opsguard/workspace.py).
        with workspace.use(ctx.user.workspace):
            result = self.handler(ctx, **args)
            if inspect.isawaitable(result):
                result = await result
        return result


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())
