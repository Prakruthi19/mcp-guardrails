"""Customer service MCP server.

Exposes two tools over stdio:
  - get_customer_record(customer_id)
  - trigger_refund(customer_id, amount, reason)

Design note on error handling (see README.md for the full explanation):
  - Malformed arguments (bad customer_id format, non-positive amount, short
    reason) are rejected with a real JSON-RPC protocol error (INVALID_PARAMS,
    -32602), by raising MCPError from inside the tool. The SDK passes MCPError
    through unchanged instead of wrapping it as a tool result.
  - A well-formed request for a customer that doesn't exist is a business
    outcome, not a malformed request, so it is raised as ToolError, which the
    SDK turns into a normal JSON-RPC success response with isError=true.

STDIO isolation: no print()/console.log-equivalent is used anywhere in this
file. All diagnostics go through `logging`, configured below to write
exclusively to stderr, so stdout carries only JSON-RPC frames.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Annotated, Any

from mcp_types import INVALID_PARAMS
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("customer-service-mcp")

CUSTOMER_ID_PATTERN = r"^CUST-\d{5}$"

# In-memory mock "database" for the assessment.
CUSTOMER_DB: dict[str, dict[str, Any]] = {
    "CUST-00001": {"customer_id": "CUST-00001", "name": "Ada Lovelace", "email": "ada@example.com", "balance": 128.50},
    "CUST-00002": {"customer_id": "CUST-00002", "name": "Grace Hopper", "email": "grace@example.com", "balance": 0.0},
}


class GetCustomerRecordArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)


class TriggerRefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    amount: float = Field(gt=0)
    reason: str = Field(min_length=10)


def _invalid_params_error(exc: ValidationError) -> MCPError:
    """Map a Pydantic ValidationError onto a JSON-RPC INVALID_PARAMS error."""
    errors = [{"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]} for err in exc.errors()]
    return MCPError(code=INVALID_PARAMS, message="Invalid tool arguments", data={"errors": errors})


mcp = MCPServer(name="customer-service", version="1.0.0")


@mcp.tool()
async def get_customer_record(
    customer_id: Annotated[str, Field(description="Customer ID in CUST-XXXXX format, e.g. CUST-00042")],
) -> dict[str, Any]:
    """Fetch a customer record by customer ID."""
    try:
        args = GetCustomerRecordArgs(customer_id=customer_id)
    except ValidationError as exc:
        logger.info("get_customer_record rejected malformed customer_id=%r", customer_id)
        raise _invalid_params_error(exc) from exc

    record = CUSTOMER_DB.get(args.customer_id)
    if record is None:
        raise ToolError(f"No customer record found for {args.customer_id}")

    print(f"DEBUG: fetching {args.customer_id}", flush=True)  # <-- the stray debug print (BUG)

    logger.info("Fetched customer record %s", args.customer_id)
    return record


@mcp.tool()
async def trigger_refund(
    customer_id: Annotated[str, Field(description="Customer ID in CUST-XXXXX format, e.g. CUST-00042")],
    amount: Annotated[float, Field(description="Refund amount; must be a positive number")],
    reason: Annotated[str, Field(description="Reason for the refund; minimum 10 characters")],
) -> dict[str, Any]:
    """Trigger a refund for a customer."""
    try:
        args = TriggerRefundArgs(customer_id=customer_id, amount=amount, reason=reason)
    except ValidationError as exc:
        logger.info("trigger_refund rejected malformed arguments")
        raise _invalid_params_error(exc) from exc

    if args.customer_id not in CUSTOMER_DB:
        raise ToolError(f"No customer record found for {args.customer_id}")

    refund_id = f"REF-{uuid.uuid4().hex[:8].upper()}"
    logger.info("Processed refund %s for %s amount=%.2f", refund_id, args.customer_id, args.amount)
    return {
        "refund_id": refund_id,
        "customer_id": args.customer_id,
        "amount": args.amount,
        "reason": args.reason,
        "status": "approved",
    }


def main() -> None:
    logger.info("Starting customer-service MCP server on stdio")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
