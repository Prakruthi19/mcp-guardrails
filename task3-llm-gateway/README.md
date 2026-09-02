# Task 3 — LLM Gateway Streaming Guardrail (PII Redaction)

An LLM Gateway proxy that streams a text-generation response back to the
client in real time while redacting emails, SSNs, and credit card numbers
(Luhn-checksum validated, to avoid false-positive redactions) as they
appear — without ever buffering the full response.

- `streaming_redactor.py` — the actual guardrail: `StreamingPIIRedactor`, a
  small, HTTP-agnostic class that takes text chunks in and hands safe,
  redacted text back out. This is the piece with real algorithmic content;
  everything else is plumbing around it.
- `mock_llm_provider.py` — a mock upstream LLM (OpenAI-style SSE streaming),
  standing in for a real provider so this runs with no API key. Its canned
  response embeds an email, SSN, and credit card number, deliberately
  streamed in 5-character pieces so the PII is guaranteed to land split
  across multiple chunks.
- `llm_gateway.py` — the actual gateway: an async proxy that pulls one
  upstream SSE event at a time, runs it through `StreamingPIIRedactor`, and
  forwards whatever's safe to emit immediately.

## Setup & run

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# terminal 1
.venv/Scripts/python.exe -m uvicorn mock_llm_provider:app --port 9100

# terminal 2
.venv/Scripts/python.exe -m uvicorn llm_gateway:app --port 8090
```

Clients call `POST http://127.0.0.1:8090/v1/chat/completions` with
`{"messages": [...], "stream": true}` and read back an SSE stream in the
same shape the upstream provider uses.

## Verification

```
.venv/Scripts/python.exe test_redactor.py       # pure unit tests, no servers needed
.venv/Scripts/python.exe test_streaming_e2e.py  # live, against both running servers
```

`test_redactor.py` feeds each PII type through the redactor at every chunk
size from 1 (character-by-character — the worst case) up to 50, and asserts
the reassembled output is **byte-for-byte identical** to running the same
regex once, non-streamed, over the whole text. `test_streaming_e2e.py` hits
the real running gateway, confirms nothing leaks, and measures inter-chunk
arrival times to prove the response is actually delivered incrementally
(time-to-first-chunk vs. total duration), not silently buffered and sent all
at once.

## The core problem, and a bug it caused

PII doesn't respect chunk boundaries. `"jane.doe@example.com"` can easily
arrive as `"jane.doe@exam"` in one chunk and `"ple.com"` in the next — regex
each chunk in isolation and you find nothing in either one.

**The standard fix** is a bounded lookback buffer: append each chunk to an
internal buffer, redact complete matches, then emit everything *except* the
last `LOOKBACK` characters (default 100 — comfortably longer than any
email/SSN/credit-card pattern), holding that tail back in case it's the
start of a pattern that continues in the next chunk. Memory stays bounded
(buffer size is always O(lookback + one chunk), never proportional to how
much of the stream has gone by), and the client still gets nearly everything
almost immediately.

**A bug that first version had:** a plain "redact whatever's in the buffer,
then hold back N characters" approach isn't actually enough on its own, and
this repo's test suite caught it mid-development. The email regex's TLD is
`[A-Za-z]{2,}` — no upper bound. If the buffer happens to end right after
`"...example.co"`, the TLD's 2-character minimum is already satisfied, and
naive redaction confidently matches and redacts `"jane.doe@example.co"` —
one character too early. The `"m"` arrives in the next chunk as ordinary
text, gets appended right after the already-emitted `[REDACTED]`, and the
client sees `[REDACTED]m` — a leaked one-character fragment of the very
thing being redacted.

**The fix** (`StreamingPIIRedactor._find_boundary`): never finalize a match
that ends exactly at the current live edge of the buffer — such a match
might still be extending. Only a match with confirmed, already-arrived text
*after* it is treated as complete. Combined with the original size-based
margin (for text that hasn't triggered a match attempt *at all* yet), this
closes both gaps: patterns not yet recognized, and patterns recognized too
early. `test_redactor.py`'s exact-match-against-non-streamed-ground-truth
assertion is written specifically so a regression like this can't silently
pass again — a weaker "does it still contain the raw PII" substring check
would not have caught a single leaked character next to the marker.

## Latency trade-off, stated explicitly

`LOOKBACK` is a genuine trade-off knob, not a free parameter:

- **Larger** → safer margin against longer/unusual patterns, but a higher
  floor on time-to-first-chunk, since nothing at all can be emitted until
  the buffer exceeds `LOOKBACK` characters — even if the leading text has no
  PII in it whatsoever. (Observed in `test_streaming_e2e.py`: ~0.7s TTFT
  against the mock provider's 5-char/20ms-delay chunking, roughly the time
  to accumulate 100 characters.)
- **Smaller** → lower TTFT, but less margin for an unusually long pattern
  (a long free-text local part before an `@`, an internationalized TLD,
  etc.) to still be safely caught.

100 is a conservative default for the three patterns in scope here (email,
SSN, credit card); a production system would likely tune this per the
actual pattern set it's guarding against, or make it configurable per
request/tenant.

## Patterns matched

Email addresses, `XXX-XX-XXXX` SSNs, and 13-19 digit credit card numbers
(optionally grouped with spaces or dashes) — still representative rather
than exhaustive (a real system would also validate SSNs against an actual
area-number table, for instance), but credit cards specifically are checked
against the **Luhn checksum**, not just "the right number of digits":

```python
def _redact_match(m: re.Match[str]) -> str:
    card = m.group(3)
    if card is not None and not _luhn_valid(re.sub(r"[ -]", "", card)):
        return m.group(0)  # card-shaped, but not a valid card number -- leave it alone
    return REDACTED
```

Without this, any 13-19 digit sequence in the right shape gets redacted —
including things that just happen to look like a card number: an order
number, a tracking ID, an invoice reference. Luhn is the actual checksum
real card numbers satisfy (the same one a payment form uses client-side to
catch a typo before it ever reaches a network call), so it's what separates
"shaped like a card number" from "is one." Skipping it means false-positive
redactions in production — a support bot mangling a legitimate order
number it never should have touched.

This interacts correctly with the streaming boundary logic without any
special-casing: a still-growing digit run sitting at the live edge of the
buffer is already deferred by `_find_boundary` (same "might still extend"
protection as the email TLD case above), so the Luhn check only ever runs
once a digit sequence has fully arrived — never on a truncated prefix.
Verified in `test_redactor.py` with both a genuine (Luhn-valid) card number,
which gets redacted, and a same-length, same-shape number that fails the
checksum, which is left untouched — at every chunk size from 1 to 50.
