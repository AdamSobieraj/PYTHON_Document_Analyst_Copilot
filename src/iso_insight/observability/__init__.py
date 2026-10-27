"""Pure observability logic: PII masking, attribute allow-lists.

No I/O, no infrastructure imports — this module is testable in
isolation and is shared by the guardrails (pipeline) and the tracing
adapter.
"""
