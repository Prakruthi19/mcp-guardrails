"""Streaming PII redactor: detects and redacts emails, SSNs, and credit card
numbers in text arriving as a sequence of chunks, without ever buffering the
whole response and without ever emitting a pattern that's been torn in half
by a chunk boundary.

The problem a naive "regex each chunk as it arrives" approach has: PII
doesn't respect chunk boundaries. An email like "alice@example.com" can
easily arrive as "alice@exam" in one chunk and "ple.com" in the next --
regex the first chunk alone and you find nothing, emit it, and the email
leaks in two harmless-looking pieces.

The fix is a bounded lookback buffer:
  1. Append the new chunk to an internal buffer.
  2. Run the redaction regex over the *whole current buffer* (cheap --
     the buffer is always small, bounded by LOOKBACK + one chunk).
  3. Emit everything except the last LOOKBACK characters. Keep those
     buffered for next time.
  4. At end-of-stream, redact and flush whatever's left.

Why holding back LOOKBACK characters is enough: after step 2, any *complete*
pattern in the buffer has already been redacted. The only thing left that
could still turn into a match is a pattern *in progress* -- and an
in-progress match, by definition, must be sitting at the very end of the
buffer (if it weren't, the regex engine would already have decided,
correctly, that it doesn't match). Since every pattern we look for has a
bounded maximum length, an in-progress match can never be longer than that
maximum, so holding back a window at least that large guarantees it's never
emitted before it's either completed and redacted, or aged out as a false
alarm.

One more subtlety the naive version of this gets wrong: patterns with
open-ended quantifiers (the email regex's TLD is `[A-Za-z]{2,}`, no upper
bound) can look "complete" on a truncated prefix. If the buffer currently
ends in "...example.co", the TLD's `{2,}` minimum is already satisfied --
regex has no way to know an "m" is about to arrive and turn it into
".com". Redacting eagerly here leaks the trailing fragment right after the
marker (`[REDACTED]m`) once the rest arrives. The fix: never finalize a
match that ends exactly at the current edge of the buffer -- only matches
with confirmed, already-arrived text after them are treated as done.
Everything else (an in-progress match, or plain text within LOOKBACK of the
edge that hasn't triggered a match yet at all) stays buffered, re-evaluated
on the next chunk.
"""

from __future__ import annotations

import re

EMAIL_RE = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
SSN_RE = r"\b\d{3}-\d{2}-\d{4}\b"
# Common card formats: 13-19 digits, optionally grouped in 4s with spaces or dashes.
CREDIT_CARD_RE = r"\b(?:\d[ -]?){12,18}\d\b"

PII_PATTERN = re.compile(f"({EMAIL_RE})|({SSN_RE})|({CREDIT_CARD_RE})")

# Longest realistic match among the patterns above is a credit card written
# with separators (~19-23 chars including spaces/dashes); an email can run
# longer in principle. 100 is a generous margin over any of them.
DEFAULT_LOOKBACK = 100

REDACTED = "[REDACTED]"


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum -- what a real card number actually satisfies,
    not just "the right number of digits". Distinguishes an actual card
    number from something that's merely card-shaped (an order number, a
    tracking ID, an invoice reference), so those aren't redacted needlessly.
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_match(m: re.Match[str]) -> str:
    """Redact email/SSN matches unconditionally; redact a credit-card-shaped
    match only if it's Luhn-valid, otherwise leave it exactly as written --
    it isn't actually a card number, just something the same shape as one.
    """
    card = m.group(3)
    if card is not None and not _luhn_valid(re.sub(r"[ -]", "", card)):
        return m.group(0)
    return REDACTED


class StreamingPIIRedactor:
    """Feed it text chunks; it hands back the portion of each chunk that's
    safe to emit immediately, with any PII already redacted. Call flush()
    once the source stream ends to get the remaining tail.
    """

    def __init__(self, lookback: int = DEFAULT_LOOKBACK) -> None:
        self._buffer = ""
        self._lookback = lookback

    def _find_boundary(self, buf: str) -> int:
        """The length of the prefix of `buf` we can safely finalize right now.

        Starts from the plain size-based margin (protects against a pattern
        that hasn't triggered a regex match at all yet), then pulls back
        further if a match either touches the buffer's live edge (might
        still extend) or straddles that margin (would otherwise get sliced
        in half).
        """
        boundary = max(0, len(buf) - self._lookback)
        for m in PII_PATTERN.finditer(buf):
            if m.end() == len(buf) or m.start() < boundary < m.end():
                return min(boundary, m.start())
        return boundary

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        boundary = self._find_boundary(self._buffer)

        parts = []
        last_end = 0
        for m in PII_PATTERN.finditer(self._buffer[:boundary]):
            parts.append(self._buffer[last_end : m.start()])
            parts.append(_redact_match(m))
            last_end = m.end()
        parts.append(self._buffer[last_end:boundary])

        emit = "".join(parts)
        self._buffer = self._buffer[boundary:]
        return emit

    def flush(self) -> str:
        # Nothing more is coming, so every match is final regardless of
        # where it ends -- the "might still extend" concern doesn't apply.
        emit = PII_PATTERN.sub(_redact_match, self._buffer)
        self._buffer = ""
        return emit
