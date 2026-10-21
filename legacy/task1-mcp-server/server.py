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

from mcp_types import INVALID_PARAMS, ToolAnnotations
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
    idempotency_key: str = Field(min_length=1)


# idempotency_key -> {"fingerprint": (customer_id, amount, reason), "result": <returned dict>}
# In-memory only, matching the mock/in-memory CUSTOMER_DB: real deployment would use
# a persistent store (e.g. Redis/SQL) with a TTL, so retries stay deduped across restarts.
_REFUND_IDEMPOTENCY_STORE: dict[str, dict[str, Any]] = {}

# Per-transaction guardrail: refunds above this are never auto-approved by the
# agent, regardless of how well-formed the request is. This is a deliberate
# business rule, not input validation, so it's enforced *after* schema
# validation succeeds and produces an "escalated" result, not a JSON-RPC
# error -- the request was valid, it's just above what this tool is allowed
# to self-approve.
REFUND_AUTO_APPROVE_LIMIT = 500.00


def _invalid_params_error(exc: ValidationError) -> MCPError:
    """Map a Pydantic ValidationError onto a JSON-RPC INVALID_PARAMS error."""
    errors = [{"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]} for err in exc.errors()]
    return MCPError(code=INVALID_PARAMS, message="Invalid tool arguments", data={"errors": errors})


mcp = MCPServer(name="customer-service", version="1.0.0")


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        open_world_hint=False,
    )
)
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

    logger.info("Fetched customer record %s", args.customer_id)
    return record


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def trigger_refund(
    customer_id: Annotated[str, Field(description="Customer ID in CUST-XXXXX format, e.g. CUST-00042")],
    amount: Annotated[float, Field(description="Refund amount; must be a positive number")],
    reason: Annotated[str, Field(description="Reason for the refund; minimum 10 characters")],
    idempotency_key: Annotated[
        str,
        Field(
            description=(
                "Caller-generated unique key for this refund attempt. Retrying the same "
                "logical refund (e.g. after a timeout) with the same key returns the "
                "original result instead of issuing a second refund."
            )
        ),
    ],
) -> dict[str, Any]:
    """Trigger a refund for a customer.

    Idempotent per idempotency_key: replaying the same key with the same
    customer_id/amount/reason returns the original refund instead of creating
    a new one, so a client-side retry can never double-refund. Reusing a key
    with different arguments is rejected as a protocol error, since that's
    almost certainly a bug on the caller's side (key collision), not a
    legitimate retry.

    Refunds above REFUND_AUTO_APPROVE_LIMIT are not auto-processed: the
    result comes back with status="escalated" instead of "approved", and no
    money moves. This is a guardrail against an agent (compromised, looped,
    or just wrong) authorizing an arbitrarily large refund on its own.
    """
    try:
        args = TriggerRefundArgs(
            customer_id=customer_id, amount=amount, reason=reason, idempotency_key=idempotency_key
        )
    except ValidationError as exc:
        logger.info("trigger_refund rejected malformed arguments")
        raise _invalid_params_error(exc) from exc

    fingerprint = (args.customer_id, args.amount, args.reason)
    cached = _REFUND_IDEMPOTENCY_STORE.get(args.idempotency_key)
    if cached is not None:
        if cached["fingerprint"] != fingerprint:
            raise MCPError(
                code=INVALID_PARAMS,
                message="idempotency_key was already used with different refund arguments",
                data={"idempotency_key": args.idempotency_key},
            )
        logger.info("Replayed refund for idempotency_key=%s (no new refund issued)", args.idempotency_key)
        return cached["result"]

    if args.customer_id not in CUSTOMER_DB:
        raise ToolError(f"No customer record found for {args.customer_id}")

    if args.amount > REFUND_AUTO_APPROVE_LIMIT:
        result = {
            "customer_id": args.customer_id,
            "amount": args.amount,
            "reason": args.reason,
            "status": "escalated",
            "message": (
                f"Refund of {args.amount:.2f} exceeds the auto-approval limit of "
                f"{REFUND_AUTO_APPROVE_LIMIT:.2f} and requires manual review. No refund has been issued."
            ),
        }
        _REFUND_IDEMPOTENCY_STORE[args.idempotency_key] = {"fingerprint": fingerprint, "result": result}
        logger.info(
            "Escalated refund for %s amount=%.2f (over %.2f limit), no refund issued",
            args.customer_id,
            args.amount,
            REFUND_AUTO_APPROVE_LIMIT,
        )
        return result

    refund_id = f"REF-{uuid.uuid4().hex[:8].upper()}"
    result = {
        "refund_id": refund_id,
        "customer_id": args.customer_id,
        "amount": args.amount,
        "reason": args.reason,
        "status": "approved",
    }
    _REFUND_IDEMPOTENCY_STORE[args.idempotency_key] = {"fingerprint": fingerprint, "result": result}
    logger.info("Processed refund %s for %s amount=%.2f", refund_id, args.customer_id, args.amount)
    return result


def main() -> None:
    logger.info("Starting customer-service MCP server on stdio")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
