# Task 2 — MCP Security Gateway Proxy

HTTP/JSON-RPC reverse proxy (FastAPI + httpx) in front of a mock downstream
MCP server, enforcing role-based access on tool calls.

- `downstream_server.py` — mock MCP server, 4 tools incl. `admin_reset_key` /
  `admin_delete_user`. No auth of its own — trusts the gateway.
- `gateway.py` — reads `Authorization: Bearer <token>` → role, applies
  policy, forwards to downstream.

## Run

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

.venv/Scripts/python.exe -m uvicorn downstream_server:app --port 9000   # terminal 1
.venv/Scripts/python.exe -m uvicorn gateway:app --port 8080             # terminal 2
```

Clients call `POST http://127.0.0.1:8080/mcp` — never the downstream server directly.

## Verify

```
.venv/Scripts/python.exe test_gateway.py
```

Covers: no/garbage token, viewer on a regular tool, viewer on an admin tool
(blocked), admin on an admin tool (allowed), malformed JSON-RPC. Downstream's
own log confirms the blocked call never actually reached it.

## Design notes

**Two layers, kept separate.** Authentication (missing/unknown token) is an
HTTP `401`, enforced by middleware before the JSON-RPC body is even parsed.
Authorization (`tools/call` with `params.name` starting `admin_`, caller
isn't `admin`) is a JSON-RPC `-32001` error, returned without ever
contacting downstream. `tools/list` always forwards transparently, per spec.

**Audit logging.** Every auth failure and tool-call decision (allowed or
blocked) is written to `audit.log` as one JSON line, tokens stored only as
a truncated SHA-256 fingerprint — never in plaintext — but still
deterministic, so the same caller's events are correlatable across the log.

**Caveat.** `tools/list` is unfiltered, so a viewer can see `admin_reset_key`
exists (just not call it) — matches the task's literal "forward
transparently" instruction; a stricter gateway might filter the list by
role too.
