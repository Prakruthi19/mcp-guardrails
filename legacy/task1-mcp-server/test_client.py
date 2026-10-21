"""Manual smoke test: drives server.py over real stdio and exercises both
success and validation-failure paths for each tool.

Run with: .venv/Scripts/python.exe test_client.py
"""

import asyncio
import sys
import uuid

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError


async def call(session: ClientSession, name: str, args: dict) -> None:
    print(f"\n--- {name}({args}) ---", file=sys.stderr)
    try:
        result = await session.call_tool(name, args)
        print(f"is_error={result.is_error} content={result.content}", file=sys.stderr)
    except MCPError as exc:
        print(f"JSON-RPC ERROR code={exc.code} message={exc.message} data={exc.data}", file=sys.stderr)


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Tools:", [t.name for t in tools.tools], file=sys.stderr)

            # Valid calls
            await call(session, "get_customer_record", {"customer_id": "CUST-00001"})
            refund_key = str(uuid.uuid4())
            refund_args = {
                "customer_id": "CUST-00001",
                "amount": 25.5,
                "reason": "Item arrived damaged",
                "idempotency_key": refund_key,
            }
            await call(session, "trigger_refund", refund_args)

            # Same idempotency_key + same args -> replay, no second refund created
            await call(session, "trigger_refund", refund_args)

            # Same idempotency_key + different args -> rejected (key collision)
            await call(session, "trigger_refund", {**refund_args, "amount": 999})

            # Over the auto-approval cap -> escalated, not auto-processed
            await call(
                session,
                "trigger_refund",
                {
                    "customer_id": "CUST-00001",
                    "amount": 750,
                    "reason": "Large defective order refund",
                    "idempotency_key": str(uuid.uuid4()),
                },
            )

            # Invalid calls -> expect JSON-RPC INVALID_PARAMS (-32602)
            await call(session, "get_customer_record", {"customer_id": "12345"})
            await call(
                session,
                "trigger_refund",
                {"customer_id": "CUST-00001", "amount": -5, "reason": "short", "idempotency_key": str(uuid.uuid4())},
            )

            # Valid format, nonexistent customer -> expect isError=True tool result
            await call(session, "get_customer_record", {"customer_id": "CUST-99999"})


if __name__ == "__main__":
    asyncio.run(main())
