# OpsGuard

**Let AI fix your servers, without the risk.**

OpsGuard sits between AI assistants and your infrastructure (Linux servers,
Docker, Slurm clusters). Every action the AI tries is checked against the
person's role, then **allowed**, **held for approval** or **blocked**.
Secrets are hidden from the output, and everything goes into a
tamper-evident audit log.

**Live demo: https://opsguard-demo.onrender.com** · simulated servers, nothing to install. Click **Try the live demo** for a 2-minute guided tour. (Free hosting: the first load after a quiet spell takes ~50 s.)

## What it does

| | |
|---|---|
| 👤 **Roles** | Viewers look. Operators fix. Admins approve. |
| ⏸ **Approvals** | Risky actions (delete logs, reboot, drain a node) wait for a second person. Nobody approves their own request. |
| 🔒 **Protected servers** | Mark production; any change there needs approval. |
| 🛡 **Secret redaction** | API keys, emails and card numbers are removed before anyone, including the AI, sees them. |
| 🔗 **Tamper-evident log** | Each entry is hash-chained to the previous one; edits are detected. |
| 🏢 **Workspaces** | Multi-tenant: each team has its own servers, approvals and audit log. |
| ⏱ **Rate limits** | A runaway AI can't hammer your machines. |

Use it from the **web console**, or from **Claude Desktop, Claude Code or
Cursor** as an MCP server. The same rules apply everywhere.

## Run it

```bash
docker compose up
# open http://localhost:8000
```

Without Docker (Python 3.11+):

```bash
pip install -e ".[dev]"
opsguard serve            # http://127.0.0.1:8000
```

Set `ANTHROPIC_API_KEY` to use Claude. Without it, a scripted assistant
stands in, going through exactly the same safety checks.

## How it works

```
 Web console   ·   AI assistant   ·   Claude Desktop / Cursor (MCP)
                         │
            ┌──────── Guard ─────────┐
            │ rate limit → role check │
            │  allow / hold / block   │
            │ hide secrets → audit    │
            └─────────────────────────┘
                         │
          Linux        Docker        Slurm
```

Every request takes one code path (`opsguard/guard.py`), so a rule enforced
for the web console is enforced for the AI and MCP clients too.

## Tech

Python · FastAPI · SQLite · Model Context Protocol (MCP) · Claude API ·
Docker · Prometheus metrics · GitHub Actions CI · vanilla JS (no build step)

## Tests

```bash
pytest -q
```

53 tests: policy rules, approval races, redaction, audit tampering, rate
limits under concurrency, workspace isolation and the full demo story end
to end.

## More

- [Design notes](docs/DESIGN.md): architecture, decisions and trade-offs
- [`opsguard.yaml`](opsguard.yaml): roles, users, workspaces and protected servers
- `legacy/`: the earlier prototypes this project grew out of
