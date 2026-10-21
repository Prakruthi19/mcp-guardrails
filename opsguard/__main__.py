"""Command line: opsguard serve | mcp | verify-audit | suggest-policy"""

from __future__ import annotations

import argparse
import asyncio
import json
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="opsguard", description="A safe AI assistant for your servers.")
    parser.add_argument("--config", help="path to opsguard.yaml (default: $OPSGUARD_CONFIG or ./opsguard.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the web UI and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp", help="run the MCP server over stdio (for Claude Desktop, Claude Code, Cursor)")
    sub.add_parser("verify-audit", help="check the audit log's hash chain for tampering")
    sub.add_parser("suggest-policy", help="suggest tool restrictions from real usage in the audit log")
    args = parser.parse_args()

    if args.config:
        os.environ["OPSGUARD_CONFIG"] = args.config

    if args.command == "serve":
        import uvicorn

        from opsguard.web.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True)
    elif args.command == "mcp":
        from opsguard.mcp_server import serve as serve_mcp

        asyncio.run(serve_mcp())
    else:
        print(json.dumps(asyncio.run(_offline(args.command)), indent=2))


async def _offline(command: str):
    from opsguard.config import load_config
    from opsguard.runtime import build_runtime
    from opsguard.safety.suggest import suggest_policy

    runtime = await build_runtime(load_config())
    try:
        if command == "verify-audit":
            return await runtime.store.verify_audit()
        entries = await runtime.store.audit_entries(0, 1_000_000)
        return suggest_policy(runtime.config, runtime.registry, entries)
    finally:
        await runtime.close()


if __name__ == "__main__":
    main()
