"""Redacts secrets and personal data from tool output.

Runs on every tool result before it is shown to the LLM, stored in the
audit log or returned to the user. Server logs are where secrets leak in
practice: an app prints its config on startup, a failed request logs an
Authorization header, a crash dumps an environment variable.

The PII patterns and the Luhn check come from the streaming redactor in
legacy/task3-llm-gateway. Tool results arrive whole rather than as a token
stream, so this version runs the patterns once over each string instead of
keeping a lookback buffer.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Order matters: the more specific patterns run first so, for example, a
# private key block is removed as one unit before the generic
# "key=value" rule can chew on its first line.
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_TOKENS = re.compile(
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"  # AWS access key id
    r"|\bsk-[A-Za-z0-9_-]{16,}"  # OpenAI/Anthropic-style API keys
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}\b"  # GitHub tokens
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"  # Slack tokens
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_text(text: str) -> tuple[str, int]:
    """Return (redacted text, number of redactions)."""
    count = 0

    def sub(pattern: re.Pattern[str], repl: Any, s: str) -> str:
        nonlocal count
        s, n = pattern.subn(repl, s)
        count += n
        return s

    text = sub(_PRIVATE_KEY, REDACTED, text)
    text = sub(_ASSIGNMENT, lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = sub(_BEARER, lambda m: f"{m.group(1)}{REDACTED}", text)
    text = sub(_TOKENS, REDACTED, text)
    text = sub(_EMAIL, REDACTED, text)
    text = sub(_SSN, REDACTED, text)

    def card(m: re.Match[str]) -> str:
        nonlocal count
        if _luhn_valid(re.sub(r"[ -]", "", m.group(0))):
            count += 1
            return REDACTED
        return m.group(0)  # card-shaped but not a card: an order id, a timestamp...

    text = _CARD.sub(card, text)
    return text, count


def redact(value: Any) -> tuple[Any, int]:
    """Redact every string inside a JSON-like value. Returns (value, count)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        total = 0
        out = {}
        for k, v in value.items():
            out[k], n = redact(v)
            total += n
        return out, total
    if isinstance(value, (list, tuple)):
        total = 0
        items = []
        for v in value:
            item, n = redact(v)
            items.append(item)
            total += n
        return items, total
    return value, 0
