"""Loads and validates opsguard.yaml.

Validation happens once at startup and fails loudly: a typo in a role rule
(say `dangerous: alow`) must stop the server, not silently fall through to
some default that might be more permissive than intended.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from opsguard import workspace as ws

RISK_LEVELS = ("read", "change", "dangerous")
ACTIONS = ("allow", "approval", "deny")
BACKENDS = ("demo", "local", "off")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    name: str
    role: str
    token: str
    # Account name on the Slurm cluster, if different from the OpsGuard name.
    slurm_user: str
    # The tenant this user belongs to; see opsguard/workspace.py.
    workspace: str = ws.DEFAULT


@dataclass
class Config:
    connectors: dict[str, str]
    llm_provider: str
    llm_model: str
    database: str
    users: dict[str, User]
    roles: dict[str, dict[str, str]]
    approvers: list[str]
    protected_targets: list[str]
    tool_overrides: dict[str, str]
    cleanup_roots: list[str]
    tool_calls_per_minute: int
    chat_messages_per_minute: int
    _by_token: dict[str, User] = field(default_factory=dict, repr=False)

    def user_for_token(self, token: str) -> User | None:
        return self._by_token.get(token)

    @property
    def workspaces(self) -> list[str]:
        return sorted({u.workspace for u in self.users.values()})

    @property
    def demo_mode(self) -> bool:
        return any(backend == "demo" for backend in self.connectors.values())


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def parse_config(raw: dict[str, Any]) -> Config:
    connectors = {name: str(backend) for name, backend in (raw.get("connectors") or {}).items()}
    for name, backend in connectors.items():
        _require(backend in BACKENDS, f"connector {name!r}: backend must be one of {BACKENDS}, got {backend!r}")

    roles: dict[str, dict[str, str]] = {}
    for role, rules in (raw.get("roles") or {}).items():
        _require(isinstance(rules, dict), f"role {role!r} must map risk levels to actions")
        for risk in RISK_LEVELS:
            action = rules.get(risk)
            _require(action in ACTIONS, f"role {role!r}: {risk} must be one of {ACTIONS}, got {action!r}")
        roles[role] = {risk: rules[risk] for risk in RISK_LEVELS}
    _require(bool(roles), "at least one role must be defined")

    users: dict[str, User] = {}
    by_token: dict[str, User] = {}
    for name, spec in (raw.get("users") or {}).items():
        _require(isinstance(spec, dict), f"user {name!r} must be a mapping")
        role = spec.get("role")
        _require(role in roles, f"user {name!r}: unknown role {role!r}")
        # Tokens can come from the environment so real ones never sit in the file.
        token = os.environ.get(spec["token_env"], "") if "token_env" in spec else str(spec.get("token", ""))
        _require(len(token) >= 8, f"user {name!r}: token must be at least 8 characters")
        _require(token not in by_token, f"user {name!r}: token is shared with another user")
        workspace = str(spec.get("workspace", ws.DEFAULT))
        _require(bool(ws.NAME.match(workspace)), f"user {name!r}: workspace must be lowercase letters, digits and dashes")
        user = User(name=name, role=role, token=token, slurm_user=str(spec.get("slurm_user", name)), workspace=workspace)
        users[name] = user
        by_token[token] = user

    approvers = list(raw.get("approvers") or [])
    for role in approvers:
        _require(role in roles, f"approvers: unknown role {role!r}")

    overrides = {str(k): str(v) for k, v in (raw.get("tool_overrides") or {}).items()}
    for tool, action in overrides.items():
        _require(action in ACTIONS, f"tool_overrides.{tool}: must be one of {ACTIONS}, got {action!r}")

    llm = raw.get("llm") or {}
    provider = str(llm.get("provider", "auto"))
    _require(provider in ("auto", "anthropic", "mock"), f"llm.provider must be auto, anthropic or mock, got {provider!r}")

    limits = raw.get("rate_limits") or {}

    return Config(
        connectors=connectors,
        llm_provider=provider,
        llm_model=str(llm.get("model", "claude-opus-5-5")),
        database=str(raw.get("database", "data/opsguard.db")),
        users=users,
        roles=roles,
        approvers=approvers,
        protected_targets=[str(t) for t in raw.get("protected_targets") or []],
        tool_overrides=overrides,
        cleanup_roots=[str(p) for p in raw.get("cleanup_roots") or []],
        tool_calls_per_minute=int(limits.get("tool_calls_per_minute", 30)),
        chat_messages_per_minute=int(limits.get("chat_messages_per_minute", 10)),
        _by_token=by_token,
    )


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    path = Path(path or os.environ.get("OPSGUARD_CONFIG", "opsguard.yaml"))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    config = parse_config(raw)
    if os.environ.get("OPSGUARD_DATABASE"):
        config.database = os.environ["OPSGUARD_DATABASE"]
    return config
