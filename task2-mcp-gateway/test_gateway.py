"""Exercises the gateway end-to-end against the running downstream server.

Requires both servers already running:
  .venv/Scripts/python.exe -m uvicorn downstream_server:app --port 9000
  .venv/Scripts/python.exe -m uvicorn gateway:app --port 8080

Run with: .venv/Scripts/python.exe test_gateway.py
"""

import httpx

GATEWAY_URL = "http://127.0.0.1:8080/mcp"


def call(label: str, token: str | None, method: str, params: dict | None = None, request_id: int = 1) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params

    resp = httpx.post(GATEWAY_URL, json=body, headers=headers)
    print(f"\n--- {label} ---")
    print(f"HTTP {resp.status_code}: {resp.json()}")


if __name__ == "__main__":
    # 1. No token at all -> 401, never reaches downstream
    call("no token", None, "tools/list")

    # 2. Unknown/garbage token -> 401
    call("garbage token", "not-a-real-token", "tools/list")

    # 3. Valid viewer token, tools/list -> forwarded transparently
    call("viewer: tools/list", "viewer-token-456", "tools/list")

    # 4. Valid viewer token, regular tool -> allowed, forwarded
    call("viewer: get_weather (regular tool)", "viewer-token-456", "tools/call", {"name": "get_weather", "arguments": {"location": "NYC"}})

    # 5. Valid viewer token, admin tool -> BLOCKED with -32001, never reaches downstream
    call("viewer: admin_reset_key (should be BLOCKED)", "viewer-token-456", "tools/call", {"name": "admin_reset_key", "arguments": {"key_id": "abc"}})

    # 6. Valid admin token, admin tool -> allowed, forwarded
    call("admin: admin_reset_key (should be ALLOWED)", "admin-token-123", "tools/call", {"name": "admin_reset_key", "arguments": {"key_id": "abc"}})

    # 7. Valid admin token, regular tool -> allowed too
    call("admin: list_users (regular tool)", "admin-token-123", "tools/call", {"name": "list_users", "arguments": {}})

    # 8. Malformed JSON-RPC (no method) -> -32600
    resp = httpx.post(GATEWAY_URL, json={"jsonrpc": "2.0", "id": 99}, headers={"Authorization": "Bearer admin-token-123"})
    print("\n--- malformed request (missing method) ---")
    print(f"HTTP {resp.status_code}: {resp.json()}")
