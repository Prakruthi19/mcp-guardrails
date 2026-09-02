"""Mock upstream LLM provider: streams a canned response as Server-Sent
Events, OpenAI-style (`data: {...}\\n\\n`, terminated by `data: [DONE]\\n\\n`).

Standing in for a real provider (OpenAI/Anthropic/etc.) so the gateway can be
exercised end-to-end without needing an API key. The gateway's redaction
logic doesn't know or care that this is a mock -- it only ever sees "an SSE
stream of text deltas," exactly the shape a real provider's streaming API
has.

The canned response deliberately embeds an email, an SSN, and a credit card
number, and is split into small (5-character) chunks with a short delay
between them -- small enough that the PII is guaranteed to land split across
multiple chunks, so this actually exercises the boundary-spanning case
instead of accidentally sidestepping it.

Run with: .venv/Scripts/python.exe -m uvicorn mock_llm_provider:app --port 9100
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="mock-llm-provider")

CANNED_RESPONSE = (
    "Sure, here is the account summary you asked for. The customer's email on file is "
    "jane.doe@example.com, their SSN is 123-45-6789 for identity verification, and the "
    "card on file is 4111 1111 1111 1111 expiring 12/29. Let me know if you need anything else."
)

CHUNK_SIZE = 5
CHUNK_DELAY_SECONDS = 0.02


async def _sse_stream():
    for i in range(0, len(CANNED_RESPONSE), CHUNK_SIZE):
        piece = CANNED_RESPONSE[i : i + CHUNK_SIZE]
        payload = {"choices": [{"delta": {"content": piece}}]}
        yield f"data: {json.dumps(payload)}\n\n"
        await asyncio.sleep(CHUNK_DELAY_SECONDS)
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> StreamingResponse:
    body = await request.json()
    if not body.get("stream"):
        # Non-streaming path, included for completeness; the gateway only uses stream=true.
        return StreamingResponse(iter([json.dumps({"choices": [{"message": {"content": CANNED_RESPONSE}}]})]))
    return StreamingResponse(_sse_stream(), media_type="text/event-stream")
