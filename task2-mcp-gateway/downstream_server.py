"""Mock downstream MCP server.

A minimal HTTP/JSON-RPC server standing in for "the real MCP server" that
the gateway proxies to. It has no auth of its own -- it trusts whatever
reaches it, exactly like a real internal service sitting behind a gateway
that's the actual security boundary. Its only job is to answer tools/list
and tools/call so the gateway (gateway.py) has something real to forward to
and to withhold from.

Run with: .venv/Scripts/python.exe -m uvicorn downstream_server:app --port 9000
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("downstream-mcp-server")

app = FastAPI(title="mock-downstream-mcp-server")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a location.",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
    {
        "name": "list_users",
        "description": "List all users in the workspace.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "admin_reset_key",
        "description": "Reset an API key. Admin only.",
        "inputSchema": {
            "type": "object",
            "properties": {"key_id": {"type": "string"}},
            "required": ["key_id"],
        },
    },
    {
        "name": "admin_delete_user",
        "description": "Delete a user account. Admin only.",
        "inputSchema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
]

# Mock results, keyed by tool name -- this server doesn't do anything real,
# it just proves the gateway forwarded (or didn't forward) the call.
MOCK_RESULTS: dict[str, Any] = {
    "get_weather": {"temperature_c": 21, "conditions": "partly cloudy"},
    "list_users": {"users": ["ada", "grace"]},
    "admin_reset_key": {"status": "key rotated"},
    "admin_delete_user": {"status": "user deleted"},
}


def _jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


@app.post("/mcp")
async def handle_jsonrpc(request: Request) -> JSONResponse:
    body = await request.json()
    request_id = body.get("id")
    method = body.get("method")

    if method == "tools/list":
        logger.info("tools/list")
        return JSONResponse(_jsonrpc_result(request_id, {"tools": TOOLS}))

    if method == "tools/call":
        params = body.get("params") or {}
        name = params.get("name")
        logger.info("tools/call name=%s", name)
        if name not in MOCK_RESULTS:
            return JSONResponse(_jsonrpc_error(request_id, -32602, f"Unknown tool: {name}"))
        return JSONResponse(
            _jsonrpc_result(request_id, {"content": [{"type": "text", "text": str(MOCK_RESULTS[name])}], "isError": False})
        )

    return JSONResponse(_jsonrpc_error(request_id, -32601, f"Method not found: {method}"))
