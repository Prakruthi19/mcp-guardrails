"""Web server: the chat UI, approvals, audit log, health and metrics."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, Field

from opsguard import __version__, workspace
from opsguard.agent import make_agent
from opsguard.config import Config, User, load_config
from opsguard.overview import snapshot
from opsguard.runtime import Runtime, build_runtime
from opsguard.safety.suggest import suggest_policy

logger = logging.getLogger("opsguard.web")
STATIC = Path(__file__).parent / "static"
CHATS = Counter("opsguard_chat_messages_total", "Chat messages by outcome", ["status"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", max_length=64)


class ToolRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


def create_app(config: Config | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = await build_runtime(config or load_config())
        app.state.runtime = runtime
        app.state.agent = make_agent(runtime.config, runtime.guard)
        logger.info("OpsGuard %s started: connectors=%s assistant=%s",
                    __version__, runtime.config.connectors, app.state.agent.name)
        yield
        await runtime.close()

    app = FastAPI(title="OpsGuard", version=__version__, lifespan=lifespan, docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def runtime(request: Request) -> Runtime:
        return request.app.state.runtime

    def current_user(request: Request) -> User:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        user = request.app.state.runtime.config.user_for_token(token) if token else None
        if user is None:
            raise HTTPException(401, "missing or unknown token")
        return user

    @app.get("/", include_in_schema=False)
    async def landing() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/app", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(STATIC / "app.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/info")
    async def info(request: Request) -> dict[str, Any]:
        rt = runtime(request)
        demo_users = []
        if rt.config.demo_mode:
            # Demo tokens are published on purpose so visitors can switch roles.
            demo_users = [{"name": u.name, "role": u.role, "token": u.token, "workspace": u.workspace}
                          for u in rt.config.users.values()]
        return {
            "version": __version__,
            "assistant": request.app.state.agent.name,
            "connectors": rt.config.connectors,
            "demo": rt.config.demo_mode,
            "demo_users": demo_users,
        }

    @app.get("/api/me")
    async def me(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        rt = runtime(request)
        rules = rt.config.roles[user.role]
        tools = [
            {"name": t.name, "connector": t.connector, "risk": t.risk,
             "access": rt.config.tool_overrides.get(t.name, rules[t.risk]), "description": t.description}
            for t in rt.registry.all()
        ]
        return {"name": user.name, "role": user.role, "slurm_user": user.slurm_user, "workspace": user.workspace,
                "can_approve": user.role in rt.config.approvers, "tools": tools}

    @app.get("/api/policy")
    async def policy(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        rt = runtime(request)
        return {
            "roles": rt.config.roles,
            "approvers": rt.config.approvers,
            "protected_targets": rt.config.protected_targets,
            "tool_overrides": rt.config.tool_overrides,
            "connectors": rt.config.connectors,
            "tools": [{"name": t.name, "risk": t.risk, "connector": t.connector, "description": t.description}
                      for t in rt.registry.all() if t.connector != "opsguard"],
        }

    @app.get("/api/overview")
    async def overview(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        return await snapshot(runtime(request), user)

    @app.post("/api/tools/{tool_name}")
    async def call_tool(tool_name: str, body: ToolRequest, request: Request,
                        user: User = Depends(current_user)) -> dict[str, Any]:
        # The dashboard's buttons: the same Guard path the assistant and MCP use.
        outcome = await runtime(request).guard.call(user, tool_name, body.args, source="web")
        return outcome.to_dict()

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        rt = runtime(request)
        if not await rt.store.hit_rate_limit(f"chat:{user.name}", rt.config.chat_messages_per_minute):
            CHATS.labels("rate_limited").inc()
            raise HTTPException(429, f"rate limit: {rt.config.chat_messages_per_minute} messages per minute")
        reply = await request.app.state.agent.run(user, body.session_id, body.message, source="web")
        CHATS.labels("ok").inc()
        return reply.to_dict()

    @app.post("/api/chat/reset")
    async def chat_reset(request: Request, user: User = Depends(current_user)) -> dict[str, str]:
        request.app.state.agent.reset(user, "default")
        return {"status": "ok"}

    @app.get("/api/approvals")
    async def approvals(request: Request, status: str | None = None, user: User = Depends(current_user)) -> list[dict[str, Any]]:
        rows = await runtime(request).store.list_approvals(status, workspace=user.workspace)
        if user.role not in runtime(request).config.approvers:
            rows = [r for r in rows if r["requester"] == user.name]
        return rows

    @app.post("/api/approvals/{approval_id}/{decision}")
    async def decide(approval_id: int, decision: str, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        guard = runtime(request).guard
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "unknown decision")
        try:
            if decision == "approve":
                return await guard.approve(user, approval_id)
            return await guard.reject(user, approval_id)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except LookupError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/audit")
    async def audit(request: Request, after: int = 0, limit: int = 100, user: User = Depends(current_user)) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        store = runtime(request).store
        if after:
            return await store.audit_entries(after, limit, workspace=user.workspace)
        return await store.recent_audit(limit, workspace=user.workspace)

    @app.get("/api/audit/verify")
    async def audit_verify(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        return await runtime(request).store.verify_audit(workspace=user.workspace)

    @app.get("/api/policy/suggest")
    async def policy_suggest(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        rt = runtime(request)
        if user.role not in rt.config.approvers:
            raise HTTPException(403, "only approvers can view policy suggestions")
        entries = await rt.store.audit_entries(0, 100_000, workspace=user.workspace)
        return suggest_policy(rt.config, rt.registry, entries)

    @app.post("/api/demo/reset")
    async def demo_reset(request: Request, user: User = Depends(current_user)) -> dict[str, str]:
        rt = runtime(request)
        if rt.demo is None:
            raise HTTPException(404, "not in demo mode")
        with workspace.use(user.workspace):
            rt.demo.reset()
        await rt.store.expire_pending(f"demo reset by {user.name}", workspace=user.workspace)
        return {"status": "reset"}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app
