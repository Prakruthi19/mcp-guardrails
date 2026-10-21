"""MCP server: use OpsGuard's tools from Claude Desktop, Claude Code, Cursor...

Runs over stdio. The person using the MCP client is identified by the
OPSGUARD_TOKEN environment variable (one of the tokens in opsguard.yaml),
and every call goes through the same Guard as the web chat: same role
rules, same approvals, same redaction, same audit log. Point it at the same
database as the web server and held actions show up in the web UI's
approval queue.

stdout carries only JSON-RPC, so all logging goes to stderr.
"""

from __future__ import annotations

import json
import logging
import os
import sys

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from opsguard.config import load_config
from opsguard.runtime import build_runtime

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("opsguard.mcp")


async def serve() -> None:
    config = load_config()
    token = os.environ.get("OPSGUARD_TOKEN", "")
    user = config.user_for_token(token)
    if user is None:
        raise SystemExit("OPSGUARD_TOKEN is missing or does not match a user in opsguard.yaml")
    runtime = await build_runtime(config)
    logger.info("OpsGuard MCP server for %s (%s)", user.name, user.role)

    async def list_tools(ctx, params) -> types.ListToolsResult:
        rules = config.roles[user.role]
        tools = []
        for t in runtime.registry.all():
            access = config.tool_overrides.get(t.name, rules[t.risk])
            if access == "deny":
                continue  # don't advertise tools this user can never use
            note = " Requires approval before it runs." if access == "approval" else ""
            tools.append(types.Tool(
                name=t.name,
                description=f"[{t.risk}] {t.description}{note}",
                inputSchema=t.input_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=t.risk == "read",
                    destructiveHint=t.risk == "dangerous",
                ),
            ))
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        outcome = await runtime.guard.call(user, params.name, params.arguments or {}, source="mcp")
        payload = {"status": outcome.status, "message": outcome.message, "result": outcome.result}
        if outcome.approval_id is not None:
            payload["approval_id"] = outcome.approval_id
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(payload, indent=2, default=str))],
            isError=outcome.status in ("error", "denied", "rate_limited"),
        )

    server = Server(
        "opsguard",
        instructions="Operations tools guarded by OpsGuard. Denied or held actions are reported in the result; do not try to work around them.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await runtime.close()
