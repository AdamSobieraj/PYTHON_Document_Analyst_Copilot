"""PII detection & masking — pure logic, stdlib only, no I/O.

Two consumers:

- the input guardrails (``pipeline``) block a query when it contains
  PII — a raw query with personal data must never reach the broker;
- the tracing adapter (``adapters/tracing``) masks values before
  anything is sent to the external tracing backend (Langfuse).

Coverage (per CLAUDE.md): e-mail, phone, IBAN, national ID numbers,
company/tax numbers. Patterns are conservative word-bounded digit runs:
a false positive at worst blocks a query, a false negative would leak
PII.
"""

from __future__ import annotations

import re

# Polish phone: with +48 prefix any separators, without it the separators
# are mandatory — so a plain 9-digit run (national ID) is not
# mislabelled as a phone number.
_PHONE = r"(?<!\d)(?:\+48[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{3}|\d{3}[\s-]\d{3}[\s-]\d{3})(?!\d)"

# (label used in the mask, compiled pattern) — most specific first, so
# overlapping digit runs are consumed by the right rule (an 11-digit
# national ID is not masked as a phone number, an IBAN is not broken
# into smaller pieces).
_PII_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){10,30}\b", re.IGNORECASE)),
    ("NATIONAL_ID", re.compile(r"\b\d{11}\b")),
    ("TAX_ID", re.compile(r"\b\d{10}\b")),
    ("PHONE", re.compile(_PHONE)),
    ("ID", re.compile(r"\b\d{9}\b")),
    ("ID", re.compile(r"\b\d{2}[-\s]?\d{2}[-\s]?\d{2}[-\s]?\d{2}\b")),
)


def contains_pii(text: str) -> bool:
    """True when at least one PII pattern matches (guardrail decision)."""
    return any(pattern.search(text) for _, pattern in _PII_RULES)


def mask_pii(text: str) -> str:
    """Replace every PII match with a bracketed label (e.g. ``[EMAIL]``).

    The result is safe to send to an external tracing backend: the
    labels keep the text readable without exposing the values.
    """
    for label, pattern in _PII_RULES:
        text = pattern.sub(f"[{label}]", text)
    return text
