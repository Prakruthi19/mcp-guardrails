# Task 2 — MCP Security Gateway Proxy

An HTTP/JSON-RPC reverse proxy (FastAPI + httpx) that sits in front of a mock
downstream MCP server and enforces role-based access on tool calls.

- `downstream_server.py` — the mock downstream MCP server. Exposes 4 tools
  (`get_weather`, `list_users`, `admin_reset_key`, `admin_delete_user`) over
  a single `POST /mcp` JSON-RPC endpoint. Has no auth of its own — it trusts
  whatever reaches it, same as a real internal service sitting behind a
  gateway that's the actual security boundary.
- `gateway.py` — the proxy. Reads `Authorization: Bearer <token>`, resolves
  it to a role, and applies policy before forwarding to the downstream
  server.

## Setup & run

Needs two terminals (or two background processes) — the downstream server
first, then the gateway:

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# terminal 1
.venv/Scripts/python.exe -m uvicorn downstream_server:app --port 9000

# terminal 2
.venv/Scripts/python.exe -m uvicorn gateway:app --port 8080
```

Clients talk to the gateway on `POST http://127.0.0.1:8080/mcp`, never to
the downstream server directly.

## Verification

```
.venv/Scripts/python.exe test_gateway.py
```

Exercises every path: no token, garbage token, viewer calling a regular
tool, viewer calling an admin tool (blocked), admin calling an admin tool
(allowed), and a malformed JSON-RPC body. The downstream server's own log
output confirms the blocked call never actually reached it — the gateway
returns `-32001` without making an outbound request at all.

## Auth model

Two layers, deliberately kept separate:

1. **Authentication (who are you), enforced for every request, as HTTP.**
   `require_authentication` is a FastAPI/Starlette middleware — it runs
   before the JSON-RPC body is even parsed. A missing `Authorization`
   header, a header that isn't `Bearer <token>`, or a token not in the
   (toy, in-memory) `TOKENS` table gets a plain **HTTP 401**, not a
   JSON-RPC error. That's deliberate: at this point there's no reliable
   JSON-RPC `id` to attach an error response to, and "who are you" is a
   transport-level question, not a protocol-level one.

   ```
   TOKENS = {"admin-token-123": "admin", "viewer-token-456": "viewer"}
   ```

2. **Authorization (are you allowed to do *this*), enforced per tool call,
   as JSON-RPC.** Once a request is authenticated, `handle_jsonrpc` inspects
   the JSON-RPC `method`:
   - `tools/list` → forwarded transparently, no role check (per the task
     spec — listing what tools exist isn't a privileged action here; see
     the caveat below).
   - `tools/call` with `params.name` starting with `admin_` → requires
     `role == "admin"`. If not, the gateway returns
     `{"error": {"code": -32001, "message": "Unauthorized Tool Call"}}`
     directly and **never contacts the downstream server** — confirmed by
     the downstream server's request log during verification.
   - Everything else (non-`admin_` tool calls, any other JSON-RPC method) →
     forwarded transparently.

Malformed JSON-RPC (unparseable body, or a body missing `method`) is
rejected with standard errors (`-32700` Parse error, `-32600` Invalid
Request) before any routing decision is made.

## Audit logging

Every authentication failure and every `tools/call` authorization decision
(allowed or blocked) is written to `audit.log` as one JSON object per line —
kept separate from the human-readable console logs, since this is what an
actual security review or incident investigation would query, and free-text
log lines aren't reliably parseable for that.

```json
{"timestamp": "...", "event": "tool_call_blocked", "tool": "admin_reset_key", "role": "viewer", "token_fingerprint": "53a3ab153221", "client_ip": "127.0.0.1", "required_role": "admin", "request_id": 1}
```

Two things worth calling out about how it's built:

- **Tokens are never logged in plaintext.** `_fingerprint()` stores a
  truncated SHA-256 hash of the token instead of the token itself, so the
  audit trail can't leak a live credential even if it's read by someone who
  shouldn't have the original secret. It's still deterministic — the same
  token always produces the same fingerprint — so events from the same
  caller are correlatable across the log (verified: a viewer's allowed
  `get_weather` call and their blocked `admin_reset_key` attempt carry the
  identical fingerprint; the admin's two calls carry a different, but
  mutually identical, fingerprint).
- **`tools/list` isn't logged.** Scope here is authorization *decisions* —
  listing tools isn't gated, so there's no decision to record. Logging
  every single request regardless of relevance is how audit logs become
  noise nobody actually reads.

`audit.log` is a local file for this demo; a real deployment would ship
these events to a SIEM/log pipeline instead of the filesystem.

## A caveat worth stating explicitly

`tools/list` is forwarded transparently and unfiltered, so a viewer token
*can* see that `admin_reset_key` exists (just not call it) — this matches
the task's literal instruction ("forward the request transparently"), but a
stricter gateway might also filter the tool list by role so admin-only
tools aren't even advertised to a non-admin client. Left out here to match
the spec exactly rather than assume a stronger policy wasn't intentional;
worth flagging as a follow-up hardening step in a real deployment.
