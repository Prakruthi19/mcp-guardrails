"""LLM Gateway: streaming PII-redaction proxy.

Sits between a client and an upstream LLM provider (mock_llm_provider.py
here; a real provider's streaming chat/completions endpoint in production).
Same request/response shape as the upstream, so a client can't tell the
difference except that PII never reaches it.

The whole point is to NOT buffer the full response before responding:
`_redacted_stream` is an async generator that pulls one upstream SSE event
at a time, runs it through StreamingPIIRedactor, and yields whatever's safe
to emit immediately -- so the client keeps receiving chunks throughout the
upstream's response instead of waiting for it to finish. Verified end-to-end
in test_streaming_e2e.py by measuring inter-chunk arrival times.

Run with: .venv/Scripts/python.exe -m uvicorn llm_gateway:app --port 8090
(mock_llm_provider.py must be running on port 9100 first)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from streaming_redactor import StreamingPIIRedactor

UPSTREAM_URL = "http://127.0.0.1:9100/v1/chat/completions"

app = FastAPI(title="llm-gateway")
_http_client = httpx.AsyncClient(timeout=30.0)


def _sse_event(content: str) -> str:
    return f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n"


async def _redacted_stream(upstream_request_body: dict) -> AsyncIterator[str]:
    redactor = StreamingPIIRedactor()

    async with _http_client.stream("POST", UPSTREAM_URL, json=upstream_request_body) as upstream:
        async for line in upstream.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line.removeprefix("data: ")

            if payload == "[DONE]":
                break

            event = json.loads(payload)
            delta = event["choices"][0]["delta"].get("content", "")
            if not delta:
                continue

            safe_text = redactor.feed(delta)
            if safe_text:
                yield _sse_event(safe_text)

    tail = redactor.flush()
    if tail:
        yield _sse_event(tail)
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> StreamingResponse:
    body = await request.json()
    body["stream"] = True  # this gateway only speaks the streaming path
    return StreamingResponse(_redacted_stream(body), media_type="text/event-stream")
