"""Unit tests for StreamingPIIRedactor, independent of any HTTP/streaming
plumbing. Feeds PII split at arbitrary and worst-case (character-by-character)
chunk boundaries.

The core assertion is exact-output equality against a non-streamed ground
truth: whatever chunk size the text arrives in, the reassembled streamed
output must be byte-for-byte identical to just running the same regex once
over the whole text. This is deliberately stronger than a "does it still
contain the raw PII" substring check -- a substring check would NOT catch a
redactor that leaks a single trailing fragment next to the marker (e.g.
"[REDACTED]m" instead of "[REDACTED]"), which is exactly the bug this test
suite caught during development (see streaming_redactor.py's docstring for
why open-ended quantifiers like the email TLD's `{2,}` make that possible).

Run with: .venv/Scripts/python.exe test_redactor.py
"""

from streaming_redactor import PII_PATTERN, StreamingPIIRedactor, _redact_match


def run_stream(text: str, chunk_size: int, lookback: int = 100) -> str:
    """Feeds `text` through the redactor in fixed-size chunks and returns the
    fully reassembled (redacted) output, exactly as a real client would see it."""
    redactor = StreamingPIIRedactor(lookback=lookback)
    output = []
    for i in range(0, len(text), chunk_size):
        output.append(redactor.feed(text[i : i + chunk_size]))
    output.append(redactor.flush())
    return "".join(output)


def check(name: str, text: str, chunk_sizes: list[int]) -> None:
    expected = PII_PATTERN.sub(_redact_match, text)  # ground truth: non-streamed, single-pass redaction
    for chunk_size in chunk_sizes:
        result = run_stream(text, chunk_size)
        assert result == expected, (
            f"[{name}] FAILED at chunk_size={chunk_size}:\n  expected={expected!r}\n  got     ={result!r}"
        )
    print(f"[{name}] OK across chunk sizes {chunk_sizes} (exact match to non-streamed ground truth)")


if __name__ == "__main__":
    # Worst case (chunk_size=1) forces every character-boundary split imaginable.
    sizes = [1, 2, 3, 5, 7, 13, 50]

    check("email", "Contact me at alice.smith@example.com for details.", sizes)
    check("ssn", "My SSN is 123-45-6789, please keep it safe.", sizes)
    check("credit_card", "Card number: 4111 1111 1111 1111 exp 12/29.", sizes)
    check(
        "multiple_pii_one_message",
        "Email jane@example.com or call re SSN 987-65-4321, card 4222222222222.",
        sizes,
    )
    check("no_pii_untouched", "This message has no sensitive data in it at all, just normal text.", sizes)
    check(
        "long_tld_and_back_to_back_patterns",
        "Reach out at first.last+tag@sub.example.museum or 555-55-5555x or 5105105105105100 thanks.",
        sizes,
    )

    # Luhn checksum: a card-shaped number that doesn't actually satisfy the
    # checksum real card numbers use isn't a card number -- e.g. an order ID
    # or tracking number that happens to be 16 digits long. Explicit
    # behavioral assertions here, not just ground-truth equivalence, since
    # the point is proving the *distinction* the checksum makes.
    valid_card_text = "Card on file: 4111 1111 1111 1111 for the order."
    invalid_card_text = "Order reference: 1234 5678 9012 3456 for the order."
    for chunk_size in sizes:
        valid_result = run_stream(valid_card_text, chunk_size)
        assert "[REDACTED]" in valid_result and "4111" not in valid_result, (
            f"[luhn_valid_card] FAILED at chunk_size={chunk_size}: {valid_result!r}"
        )
        invalid_result = run_stream(invalid_card_text, chunk_size)
        assert invalid_result == invalid_card_text, (
            f"[luhn_invalid_lookalike] FAILED at chunk_size={chunk_size}: "
            f"expected untouched text, got {invalid_result!r}"
        )
    print(f"[luhn_valid_card] OK across chunk sizes {sizes} (valid card number redacted)")
    print(f"[luhn_invalid_lookalike] OK across chunk sizes {sizes} (card-shaped non-card number left untouched)")

    print("\nAll redactor tests passed.")
