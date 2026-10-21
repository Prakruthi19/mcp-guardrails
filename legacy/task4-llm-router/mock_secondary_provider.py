"""Mock secondary (backup/failover) model provider. Also controllable via
`simulate`, so a test can force the "both providers fail" scenario.

Run with: .venv/Scripts/python.exe -m uvicorn mock_secondary_provider:app --port 9300
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="mock-secondary-provider")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    body = await request.json()
    simulate = body.get("simulate_secondary", "success")

    if simulate == "error":
        return JSONResponse(status_code=500, content={"error": "internal secondary provider failure, stack trace etc."})

    return JSONResponse(
        {
            "choices": [{"message": {"content": "This is the SECONDARY (failover) provider's response."}}],
            "usage": {"total_tokens": body.get("max_tokens", 100)},
        }
    )
