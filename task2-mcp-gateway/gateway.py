"""MCP Security Gateway Proxy.

Sits between an AI agent client and the downstream MCP server
(downstream_server.py). Every request must carry a recognized
`Authorization: Bearer <token>` header, which maps to a role (admin/viewer).
That's authentication -- a request either has a valid identity or it
doesn't, checked once, up front.

On top of that, tool calls whose name starts with `admin_` additionally
require the admin role -- that's authorization, decided per tool call, using
the JSON-RPC-level error the task specifies (-32001) instead of an HTTP
status, since the request itself is well-formed and authenticated; it's
just not allowed to do *this specific thing*. tools/list always passes
through untouched, since listing what exists isn't a privileged action here.

Run with: .venv/Scripts/python.exe -m uvicorn gateway:app --port 8080
(downstream_server.py must be running on port 9000 first)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("mcp-gateway")

# Structured audit trail, kept separate from the operational log above: one
# JSON object per line, written only to audit.log (propagate=False so it
# doesn't also spam the console via the root handler). This is what a real
# security review or incident investigation would actually query -- "every
# tool call a given token attempted, allowed or blocked, in order" -- which
# free-text log lines like the ones above aren't reliably parseable for.
audit_logger = logging.getLogger("mcp-gateway.audit")
audit_logger.propagate = False
audit_logger.setLevel(logging.INFO)
_audit_handler = logging.FileHandler("audit.log", encoding="utf-8")
_audit_handler.setFormatter(logging.Formatter("%(message)s"))
audit_logger.addHandler(_audit_handler)


def _fingerprint(token: str) -> str:
    """One-way, truncated identifier for a token -- enough to correlate
    events from the same caller across the audit trail, without the audit
    log itself becoming a copy of every valid credential ever presented."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def audit(event: str, **fields: Any) -> None:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    audit_logger.info(json.dumps(entry))


DOWNSTREAM_URL = "http://127.0.0.1:9000/mcp"
UNAUTHORIZED_TOOL_CALL = -32001

# Toy token->role table standing in for a real identity provider (OAuth
# introspection, JWT verification, etc.). Swapping this out is the only
# change a real deployment would need -- everything downstream of
# `resolve_role` just consumes a role string.
TOKENS: dict[str, str] = {
    "admin-token-123": "admin",
    "viewer-token-456": "viewer",
}

app = FastAPI(title="mcp-security-gateway")
_http_client = httpx.AsyncClient(timeout=10.0)


def _jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def resolve_role(request: Request) -> tuple[str | None, str | None]:
    """Extract the bearer token from the Authorization header and map it to a role.

    Returns (token, role). role is None if the header is missing,
    malformed, or the token isn't recognized -- the caller decides what to
    do with an unresolved role. token is None only if there was no bearer
    token to extract in the first place (missing/malformed header).
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, None
    token = auth_header.removeprefix("Bearer ").strip()
    return token, TOKENS.get(token)


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    """Authentication gate: every request needs a token that maps to a known role.

    This runs before the JSON-RPC body is even inspected, so a rejection
    here is a plain HTTP 401, not a JSON-RPC error -- there's no reliable
    request id to attach a JSON-RPC error to yet, and "who are you" is a
    transport-level question, not a protocol-level one.
    """
    token, role = resolve_role(request)
    client_ip = request.client.host if request.client else None
    if role is None:
        logger.info("Rejected request with missing/unknown bearer token: %s %s", request.method, request.url.path)
        audit(
            "authentication_failed",
            path=request.url.path,
            client_ip=client_ip,
            token_fingerprint=_fingerprint(token) if token else None,
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized: missing or unrecognized bearer token"},
        )
    request.state.role = role
    request.state.token_fingerprint = _fingerprint(token)
    request.state.client_ip = client_ip
    return await call_next(request)


@app.post("/mcp")
async def handle_jsonrpc(request: Request) -> JSONResponse:
    role: str = request.state.role
    token_fp: str = request.state.token_fingerprint
    client_ip = request.state.client_ip

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error: invalid JSON"))

    if not isinstance(body, dict) or "method" not in body:
        return JSONResponse(_jsonrpc_error(body.get("id") if isinstance(body, dict) else None, -32600, "Invalid Request"))

    request_id = body.get("id")
    method = body.get("method")

    if method == "tools/call":
        params = body.get("params") or {}
        tool_name = params.get("name", "")
        requires_admin = tool_name.startswith("admin_")

        if requires_admin and role != "admin":
            logger.info("Blocked tool call name=%s role=%s (admin required)", tool_name, role)
            audit(
                "tool_call_blocked",
                tool=tool_name,
                role=role,
                token_fingerprint=token_fp,
                client_ip=client_ip,
                required_role="admin",
                request_id=request_id,
            )
            return JSONResponse(
                _jsonrpc_error(
                    request_id,
                    UNAUTHORIZED_TOOL_CALL,
                    "Unauthorized Tool Call",
                    data={"tool": tool_name, "required_role": "admin", "role": role},
                )
            )

        audit(
            "tool_call_allowed",
            tool=tool_name,
            role=role,
            token_fingerprint=token_fp,
            client_ip=client_ip,
            required_admin=requires_admin,
            request_id=request_id,
        )

    # tools/list, authorized tools/call, and anything else the task doesn't
    # define a policy for: forward transparently.
    logger.info("Forwarding method=%s role=%s", method, role)
    downstream_response = await _http_client.post(DOWNSTREAM_URL, json=body)
    return JSONResponse(downstream_response.json(), status_code=downstream_response.status_code)
