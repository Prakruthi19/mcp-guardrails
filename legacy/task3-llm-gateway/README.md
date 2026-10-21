# Task 3 — LLM Gateway Streaming Guardrail (PII Redaction)

Streams an LLM response back to the client while redacting emails, SSNs,
and credit card numbers (Luhn-validated) as they appear — no full-response
buffering.

- `streaming_redactor.py` — `StreamingPIIRedactor`, the actual guardrail.
  HTTP-agnostic, independently tested.
- `mock_llm_provider.py` — mock upstream SSE stream, no API key needed.
  Streams PII deliberately split across chunk boundaries.
- `llm_gateway.py` — async proxy wiring the redactor into the live stream.

## Run

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

.venv/Scripts/python.exe -m uvicorn mock_llm_provider:app --port 9100   # terminal 1
.venv/Scripts/python.exe -m uvicorn llm_gateway:app --port 8090         # terminal 2
```

Call `POST http://127.0.0.1:8090/v1/chat/completions` with `{"messages": [...], "stream": true}`.

## Verify

```
.venv/Scripts/python.exe test_redactor.py       # unit tests, no servers needed
.venv/Scripts/python.exe test_streaming_e2e.py  # live, against both servers
```

`test_redactor.py` checks every chunk size 1–50 produces output
byte-identical to a non-streamed ground truth. `test_streaming_e2e.py`
confirms nothing leaks and measures inter-chunk timing to prove the
response is actually delivered incrementally, not buffered.

## Design notes

**Bounded lookback buffer.** Redact whatever's in the buffer, hold back the
last `LOOKBACK` (100) chars in case they're the start of a pattern
continuing in the next chunk. Keeps memory bounded, low TTFT.

**A bug the test suite caught.** The email TLD regex (`{2,}`, no upper
bound) can "complete" on a truncated prefix — e.g. redacting
`jane.doe@example.co` one character early, leaking `[REDACTED]m` once the
`m` arrives. Fix: never finalize a match that ends exactly at the buffer's
live edge — only matches with confirmed text *after* them are final. The
unit tests now assert exact byte-for-byte match against a non-streamed
ground truth specifically so a regression like this can't silently pass
(a plain "does it still contain the raw PII" check wouldn't have caught it).

**Luhn checksum on credit cards.** A 13–19 digit sequence in the right
shape (order ID, tracking number) isn't necessarily a card number — only
redacted if it passes the Luhn checksum real card numbers satisfy. Verified
with both a valid card (redacted) and a same-shape invalid one (left alone).

**Trade-off.** `LOOKBACK` size trades safety margin against TTFT floor —
larger catches longer patterns but delays the first emittable chunk; 100 is
a conservative default for these three pattern types.
