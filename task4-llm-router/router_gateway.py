"""Resilient LLM Gateway model router.

Sits in front of a primary and a secondary (backup) model provider.
Enforces a per-tenant token-aware sliding window rate limit (SQLite-backed,
see rate_limiter.py) before ever contacting a provider, calls the primary
with a hard 3000ms budget, and automatically fails over to the secondary if
the primary returns 429 or blows that budget. Every error response --
whether from the rate limiter, a provider failure, or something unexpected
-- comes back in one standardized shape with no upstream stack traces or
internal details in it; those go to the server-side log instead.

Run with: .venv/Scripts/python.exe -m uvicorn router_gateway:app --port 8070
(mock_primary_provider.py on 9200 and mock_secondary_provider.py on 9300
must both be running first)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rate_limiter import RateLimitExceeded, SlidingWindowTokenLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("llm-router")

PRIMARY_URL = "http://127.0.0.1:9200/v1/chat/completions"
SECONDARY_URL = "http://127.0.0.1:9300/v1/chat/completions"
PRIMARY_TIMEOUT_SECONDS = 3.0
SECONDARY_TIMEOUT_SECONDS = 10.0  # generous -- this is the last resort, not itself failed over
DEFAULT_TOKEN_ESTIMATE = 100
DB_PATH = "rate_limits.db"

limiter = SlidingWindowTokenLimiter(DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await limiter.start()
    app.state.http_client = httpx.AsyncClient()
    yield
    await app.state.http_client.aclose()
    await limiter.close()


app = FastAPI(title="llm-gateway-router", lifespan=lifespan)


class UpstreamTimeout(Exception):
    pass


class UpstreamRateLimited(Exception):
    pass


class UpstreamError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"upstream returned {status_code}")


def gateway_error(status_code: int, code: str, message: str) -> JSONResponse:
    """The one standardized error shape every failure path returns.
    Deliberately never includes exc details, upstream response bodies, or
    tracebacks -- those are logged server-side (logger.exception /
    logger.warning below), never put in the response.
    """
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


async def call_provider(client: httpx.AsyncClient, url: str, body: dict[str, Any], timeout_seconds: float) -> dict:
    try:
        resp = await asyncio.wait_for(client.post(url, json=body), timeout=timeout_seconds)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise UpstreamTimeout(url)
    if resp.status_code == 429:
        raise UpstreamRateLimited(url)
    if resp.status_code >= 400:
        raise UpstreamError(resp.status_code)
    return resp.json()


@app.post("/v1/chat/completions")
async def completions(request: Request) -> JSONResponse:
    tenant_key = request.headers.get("x-tenant-key")
    if not tenant_key:
        return gateway_error(401, "MISSING_TENANT_KEY", "X-Tenant-Key header is required")

    body = await request.json()
    requested_tokens = int(body.get("max_tokens", DEFAULT_TOKEN_ESTIMATE))

    try:
        reservation_id = await limiter.try_reserve(tenant_key, requested_tokens)
    except RateLimitExceeded as exc:
        logger.info("Rate limit exceeded for tenant=%s used=%d/%d", tenant_key, exc.used, exc.limit)
        return gateway_error(
            429, "RATE_LIMIT_EXCEEDED", f"Token budget exceeded for this window ({exc.used}/{exc.limit} used)"
        )

    client: httpx.AsyncClient = request.app.state.http_client
    provider_used: str
    result: dict[str, Any]

    try:
        result = await call_provider(client, PRIMARY_URL, body, PRIMARY_TIMEOUT_SECONDS)
        provider_used = "primary"
    except (UpstreamTimeout, UpstreamRateLimited) as primary_exc:
        logger.warning("Primary provider failed (%s) -- failing over to secondary", type(primary_exc).__name__)
        try:
            result = await call_provider(client, SECONDARY_URL, body, SECONDARY_TIMEOUT_SECONDS)
            provider_used = "secondary"
        except Exception:
            await limiter.release(reservation_id)
            logger.exception("Secondary provider also failed after primary failover")
            return gateway_error(502, "ALL_PROVIDERS_UNAVAILABLE", "No model provider could serve this request")
    except UpstreamError as exc:
        # Deliberately scoped to match the task spec: only 429/timeout are
        # failover triggers. A non-429 error status is a provider-side
        # problem, not a capacity problem, so retrying it against a second
        # provider isn't assumed safe here -- surfaced directly instead.
        await limiter.release(reservation_id)
        logger.warning("Primary provider returned non-failover error status=%d", exc.status_code)
        return gateway_error(502, "UPSTREAM_ERROR", "The primary model provider returned an error")
    except Exception:
        await limiter.release(reservation_id)
        logger.exception("Unexpected error calling primary provider")
        return gateway_error(500, "INTERNAL_ERROR", "An unexpected error occurred")

    return JSONResponse({"provider": provider_used, **result})
