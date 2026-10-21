"""Mock primary model provider: controllable via a `simulate` field in the
request body so tests can deterministically trigger each failure mode the
router has to handle.

  simulate: "success" (default) -> normal canned completion
  simulate: "429"                -> HTTP 429 Too Many Requests immediately
  simulate: "timeout"            -> sleeps 5s (longer than the router's 3000ms
                                     budget) so the router's own timeout fires
  simulate: "error"              -> HTTP 500 with a fake internal stack trace
                                     in the body, to prove the router never
                                     lets this leak through to the client

Run with: .venv/Scripts/python.exe -m uvicorn mock_primary_provider:app --port 9200
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="mock-primary-provider")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    body = await request.json()
    simulate = body.get("simulate", "success")

    if simulate == "429":
        return JSONResponse(status_code=429, content={"error": "rate limited upstream"})

    if simulate == "timeout":
        await asyncio.sleep(5.0)
        return JSONResponse({"choices": [{"message": {"content": "too slow, should never be seen"}}]})

    if simulate == "error":
        # Deliberately leaky-looking payload -- proves the router sanitizes
        # this before it ever reaches a client, rather than passing it through.
        return JSONResponse(
            status_code=500,
            content={
                "error": "Traceback (most recent call last):\n  File \"provider.py\", line 42, in handle\n"
                "    raise DatabaseConnectionError('postgres://internal-db:5432 unreachable')"
            },
        )

    return JSONResponse(
        {
            "choices": [{"message": {"content": "This is the primary provider's response."}}],
            "usage": {"total_tokens": body.get("max_tokens", 100)},
        }
    )
