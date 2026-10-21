# OpsGuard: design notes

**An AI assistant for your servers that only does what it's allowed to.**

Teams are starting to let AI assistants check servers, read logs and restart
things. The risk is obvious: an assistant with shell-level access will
eventually be asked, or tricked into, something it shouldn't do. OpsGuard
puts a safety layer between the assistant and your infrastructure:

- **Roles.** Viewers can look, operators can make routine changes, and
  nobody runs a destructive action on their own.
- **Approvals.** Dangerous actions (deleting files, rebooting, draining a
  cluster node) and anything touching a protected server are held until
  an approver says yes. Nobody can approve their own request.
- **Redaction.** Secrets, API keys, emails and card numbers are removed from
  tool output before the AI or the user ever sees them.
- **Tamper-evident audit log.** Every action is recorded with who asked,
  what ran and what was decided, in a hash chain that detects edits.
- **Rate limits.** Per-user limits on chat messages and tool calls stop a
  runaway loop from hammering your servers.

It works with Linux hosts, Docker and Slurm clusters, on cloud VMs,
on-premises or in a homelab. You can use it from its own web UI, or from
Claude Desktop, Claude Code or Cursor over MCP.

> **Live demo:** https://opsguard-demo.onrender.com. The demo runs against
> **simulated** servers and a simulated Slurm cluster, so you can try
> anything safely.

## Try it

```bash
docker compose up
# open http://localhost:8000
```

Or without Docker (Python 3.11+):

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
opsguard serve                          # http://127.0.0.1:8000
```

With no `ANTHROPIC_API_KEY` set, the assistant is a scripted stand-in that
understands the demo scenarios. It goes through exactly the same safety
layer as the real model, so the blocks, approvals and redactions are real.
Set the key to use Claude (`claude-opus-5-5` by default) and ask anything.

### Demo walkthrough

`/` is the product page and `/app` is the console: Overview, Servers,
Cluster, Approvals, Audit log, Access (the role matrix) and Integrations
(MCP setup). A **Live decisions** panel shows every request passing or
failing the guard, and every action button shows what the current user may
do there (⏸ needs approval, ✕ blocked). Click **Guided tour** for the story
below, or switch users from the menu in the top right:

| As | Ask | What happens |
|---|---|---|
| bob (operator) | Why is the website down on web-01? | Finds `/var` 97% full and nginx crashing with "No space left on device" |
| bob | Restart nginx on web-01 | Allowed, but nginx exits again: the disk is still full |
| bob | Clean up old logs on web-01 | Deleting files is dangerous: **held for approval** |
| carol (admin) | Approvals tab → Approve | Cleanup runs as bob; disk drops to ~50% |
| bob | Restart nginx on web-01 | Now it stays up |
| alice (viewer) | Restart nginx on web-01 | **Blocked**: viewers can't make changes |
| alice | Show the api container logs on api-01 | Stripe key, AWS key, email and card number are **redacted** |
| bob | Restart the postgresql service on db-01 | db-01 is protected: **held for approval** |
| bob | Why is job 4521 pending? | Explains it's waiting for GPUs because gpu-02 is drained |
| bob | Cancel job 4522 | **Blocked**: that's alice's job |

Then open the Activity tab and click **Verify audit log integrity**.

## How it works

```
 Dashboard buttons      AI assistant (Claude or scripted)      Claude Desktop / Cursor (MCP)
              └────────────────────┬─────────────────────────────┘
                                   │  every tool call goes through ↓
   ┌──────────────── Guard ────────────────┐
   │ rate limit → validate args → policy   │
   │   allow ──────────► run the tool      │
   │   approval ───────► approval queue    │
   │   deny ───────────► refuse            │
   │ redact output → append to audit chain │
   └───────────────────────────────────────┘
              │
   Connectors:  linux   docker   slurm
   Backends:    demo (simulated) │ local (this machine)
```

Every path - web chat, MCP, an approved request - goes through the same
`Guard.call` (`opsguard/guard.py`), so a rule enforced in one place is
enforced everywhere. The assistant never gets a direct handle on a tool.

Each tool declares a risk level, and each role maps risk levels to
`allow`, `approval` or `deny` in `opsguard.yaml`:

```yaml
roles:
  viewer:   {read: allow, change: deny,  dangerous: deny}
  operator: {read: allow, change: allow, dangerous: approval}
  admin:    {read: allow, change: allow, dangerous: approval}
approvers: [admin]
protected_targets: [db-01]   # any change here needs approval
```

Some rules only a connector can know. A Slurm user, for example, may cancel
only their own jobs. Connectors enforce these and the Guard records them as
denials like any other.

### Design notes

- **Policy is a pure function** (`opsguard/safety/policy.py`): no I/O, the
  same answer for the same inputs, and every rule is unit-tested. Protected
  targets can only tighten a decision, never loosen one.
- **Approvals run with the requester's identity**, not the approver's. A
  conditional `UPDATE … WHERE status = 'pending'` means two admins
  approving at the same moment run the action once, not twice (tested).
- **The audit log is a hash chain.** Each entry includes the hash of the
  previous one, so editing or deleting a past entry is detected
  (`opsguard verify-audit`). It stops quiet after-the-fact edits. It does not
  stop someone who rewrites the whole file, so for that, ship the log
  somewhere else.
- **One SQLite connection per process plus an `asyncio.Lock`.** SQLite's own
  locking only serializes separate connections, and many asyncio tasks
  sharing one connection look like a single client to it. An earlier
  version of the rate limiter hung under 50 concurrent requests for exactly
  this reason. The test suite now fires 50 concurrent calls at a limit of 10
  and checks that exactly 10 succeed.
- **Commands are argument lists, never shell strings**, and names are
  validated, so an AI-chosen container name like `nginx; rm -rf /` stays a
  (nonexistent) container name.
- **Tool output is treated as data.** The system prompt tells the model
  that instructions inside logs are not instructions. The real protection
  is that the Guard checks every call regardless of what the model was
  persuaded to try.
- **Workspaces (multi-tenant).** Each user belongs to a workspace
  (`workspace: acme` in `opsguard.yaml`). A workspace has its own approval
  queue, its own audit hash chain and, in the demo, its own copy of the
  servers. Another workspace's approval request answers exactly like a
  missing one, so tenants can't even learn it exists. The current
  workspace travels in a `ContextVar` that `Tool.run` sets from the caller,
  so connectors are scoped without passing a tenant ID through every
  function. The demo has two: Acme (alice, bob, carol) and Globex (dave,
  erin). Roles and policy are shared across workspaces for now.
- **The dashboard is monitoring, not action.** The live server view polls
  read tools every few seconds, so it isn't written to the audit log, but
  it only shows what the user's role may read, and its output is redacted
  too. Every button that does something goes through the Guard and is logged.
- **Least-privilege suggestions.** `opsguard suggest-policy` reads the audit
  log and lists the tools each role actually used. Tools nobody used are
  suggested as `deny` overrides ("learn, then lock").

## Use it from Claude Desktop, Claude Code or Cursor

OpsGuard is also an MCP server. Each person's MCP client sends their own
token, and only the tools their role can use are advertised:

```json
{
  "mcpServers": {
    "opsguard": {
      "command": "opsguard",
      "args": ["mcp"],
      "env": {
        "OPSGUARD_TOKEN": "demo-bob",
        "OPSGUARD_CONFIG": "/path/to/opsguard.yaml"
      }
    }
  }
}
```

Point the web server and the MCP server at the same database. Actions held
from Claude Desktop then show up in the web UI's approval queue, and the
`approval_status` tool reports back when they've run.

## Using it on real servers

1. Copy `opsguard.yaml`, set the connectors to `local`, and replace the
   demo users with real ones. Use `token_env:` to read tokens from
   environment variables instead of the file.
2. Run OpsGuard on the machine you want to manage (or a Slurm login node).
   `local` mode uses `psutil`, `systemctl`/`journalctl`, the `docker` CLI and
   the Slurm CLIs (`squeue`, `sinfo`, `scontrol`, `scancel`).
3. Put it behind HTTPS (Caddy, nginx or your platform's load balancer).

Status of the `local` backends: the Linux host overview and the Docker
connector have been run against a real machine. The Slurm `local` backend
is written against the documented CLI output formats but **has not yet been
tested on a real cluster**.

## Commands

```bash
opsguard serve [--host 0.0.0.0 --port 8000]   # web UI + API (docs at /api/docs)
opsguard mcp                                  # MCP server over stdio
opsguard verify-audit                         # check the audit hash chain
opsguard suggest-policy                       # least-privilege suggestions from usage
```

`/healthz` is for load balancers, and `/metrics` serves Prometheus metrics:
tool calls by outcome, tool latency, redactions and approvals.

## Tests

```bash
pytest -q
```

The suite covers the policy rules, the approval flow (including
self-approval and double-approval races), redaction, the audit chain
(edited and deleted entries), rate limits under concurrency, Slurm job
ownership, and the full demo story end to end through the HTTP API. CI
runs the tests, builds the Docker image and smoke-tests it.

## How this compares to managed offerings

Cloud platforms offer pieces of this for agents running on them. Amazon
Bedrock AgentCore Gateway + Policy, for example, enforces Cedar policies on
tool calls. OpsGuard is a small, self-hosted take on the same idea, aimed
at teams that run their own Linux servers, containers and Slurm clusters
anywhere. It works with any MCP client, and it can suggest a policy from
observed usage.

## Project layout

```
opsguard/
  guard.py            the single path every tool call goes through
  safety/             policy, redaction, audit/approval/rate-limit store, policy suggestions
  connectors/         linux, docker, slurm (+ the simulated demo environment)
  agent/              Claude tool-use loop, scripted demo assistant
  web/                FastAPI app and the single-page UI
  mcp_server.py       MCP server over stdio
tests/
legacy/               the earlier standalone prototypes this grew out of
```

## Roadmap

- Ship the audit log to an external sink (syslog, S3) so the chain can't be rewritten in place
- SSH backend for managing other machines, not just the one OpsGuard runs on
- Kubernetes connector
- SSO (OIDC) instead of static tokens
