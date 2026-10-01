"""Input guardrails: block empty queries, personal data and prompt injection.

This runs at the API boundary — submit_query in iso_multiagent_api.py
calls it before the task is enqueued, so a blocked query is rejected with
a 400 and never reaches the broker or a worker. The graph itself has no
guardrail node (see graph.py).
"""

from __future__ import annotations

import re

from config import PII_PATTERNS, INJECTION_KEYWORDS
from state import WorkflowState, append_audit
from tracing import observe as langfuse_observe


@langfuse_observe(name="agent.input_guard")
def input_guardrails(state: WorkflowState) -> WorkflowState:
    """Validate the user query before anything else runs.

    Checks, in order:
    1. the query is not empty,
    2. it contains no PII (e-mail-like strings, long digit sequences),
    3. it contains no prompt-injection keywords.

    On failure: sets blocked=True, block_reason and a user-facing
    final_answer, and records an "input_blocked" audit event.
    On success: clears the blocked flag and records "input_allowed".
    """
    state.setdefault("iteration", 0)
    state.setdefault("audit_log", [])

    query = state.get("query", "")

    if not query or not query.strip():
        state["blocked"] = True
        state["block_reason"] = "empty_query"
        state["final_answer"] = "Podaj pytanie dotyczące standardu ISO, procesu lub produktu."
        return append_audit(state, "input_blocked", reason=state["block_reason"])

    if any(re.search(pattern, query, re.IGNORECASE) for pattern in PII_PATTERNS):
        state["blocked"] = True
        state["block_reason"] = "pii_detected"
        state["final_answer"] = (
            "Pytanie zawiera potencjalne dane osobowe lub wrażliwe. "
            "Usuń je i zapytaj ponownie."
        )
        return append_audit(state, "input_blocked", reason=state["block_reason"])

    lowered = query.lower()
    if any(keyword in lowered for keyword in INJECTION_KEYWORDS):
        state["blocked"] = True
        state["block_reason"] = "possible_prompt_injection"
        state["final_answer"] = (
            "Wykryto potencjalną próbę modyfikacji instrukcji systemu. "
            "Zapytaj ponownie w standardowy sposób."
        )
        return append_audit(state, "input_blocked", reason=state["block_reason"])

    state["blocked"] = False
    return append_audit(state, "input_allowed")
