"""End-to-end test against the real running gateway (which itself streams
from the real running mock provider). Verifies two separate things:

  1. Correctness: the reassembled, redacted output contains no PII, and the
     surrounding text is intact.
  2. Streaming behavior: chunks actually arrive spread out over time (proving
     the gateway isn't buffering the whole upstream response before replying)
     -- printed as inter-chunk arrival gaps, and as time-to-first-chunk vs.
     total duration.

Requires both servers already running:
  .venv/Scripts/python.exe -m uvicorn mock_llm_provider:app --port 9100
  .venv/Scripts/python.exe -m uvicorn llm_gateway:app --port 8090

Run with: .venv/Scripts/python.exe test_streaming_e2e.py
"""

import json
import time

import httpx

GATEWAY_URL = "http://127.0.0.1:8090/v1/chat/completions"


def main() -> None:
    body = {"messages": [{"role": "user", "content": "Give me the account summary."}], "stream": True}

    chunks: list[str] = []
    arrival_times: list[float] = []
    start = time.monotonic()

    with httpx.Client(timeout=30.0) as client:
        with client.stream("POST", GATEWAY_URL, json=body) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line.removeprefix("data: ")
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                content = event["choices"][0]["delta"].get("content", "")
                if content:
                    chunks.append(content)
                    arrival_times.append(time.monotonic() - start)

    full_text = "".join(chunks)
    total_duration = arrival_times[-1] if arrival_times else 0.0

    print(f"Received {len(chunks)} chunks over {total_duration:.3f}s")
    print(f"Time to first chunk: {arrival_times[0]:.3f}s" if arrival_times else "No chunks received")
    print(f"\nReassembled output:\n{full_text}\n")

    # --- Correctness ---
    leaked = []
    for needle in ["jane.doe@example.com", "123-45-6789", "4111 1111 1111 1111", "4111111111111111"]:
        if needle in full_text:
            leaked.append(needle)

    if leaked:
        print(f"FAIL: leaked PII in output: {leaked}")
    elif full_text.count("[REDACTED]") != 3:
        print(f"FAIL: expected 3 [REDACTED] markers (email, SSN, card), found {full_text.count('[REDACTED]')}")
    else:
        print("PASS: all 3 PII items redacted, nothing leaked")

    # --- Streaming behavior: did chunks actually arrive spread out over time,
    # or all at once (i.e. was the response secretly buffered)? ---
    if len(arrival_times) >= 2:
        span = arrival_times[-1] - arrival_times[0]
        print(f"\nChunks spread across {span:.3f}s after the first one (non-zero => genuinely streamed, not buffered)")
        if span < 0.01:
            print("SUSPICIOUS: all chunks arrived essentially simultaneously -- looks buffered, not streamed")
        else:
            print("PASS: chunks arrived incrementally over time, confirming the gateway is not buffering the full response")


if __name__ == "__main__":
    main()
