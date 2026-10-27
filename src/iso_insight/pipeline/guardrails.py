"""Input guardrails — block a query before it reaches the workflow.

Pure logic: PII detection is the shared ``observability.masking``
module (CLAUDE.md: masking is pure logic, used both here and by the
tracing adapter); the prompt-injection keyword list is local. The
tracing span (``agent.input_guard``) is attached by the caller — the
domain never imports tracing infrastructure.
"""

from __future__ import annotations

from iso_insight.observability.masking import contains_pii
from iso_insight.schemas.workflow import WorkflowState, append_audit

# Case-insensitive substrings that indicate an attempted instruction
# override. Kept intentionally broad: a false positive blocks a query
# with a friendly message, a false negative would reach the LLM.
INJECTION_KEYWORDS: tuple[str, ...] = (
    "ignore previous",
    "ignore all",
    "system prompt",
    "prompt injection",
    "jailbreak",
    "bypass",
    "wygeneruj hasło",
    "pokaż hasło",
    "pomiń zabezpieczenia",
)


def input_guardrails(state: WorkflowState) -> WorkflowState:
    """Set ``blocked``/``block_reason`` (and the final answer) when a query must not run.

    Also initialises ``iteration`` and ``audit_log`` for the run and
    appends an ``input_allowed`` / ``input_blocked`` audit event.
    """
    state.setdefault("iteration", 0)
    state.setdefault("audit_log", [])
    query = state.get("query", "")
    if not query or not query.strip():
        state["blocked"] = True
        state["block_reason"] = "empty_query"
        state["final_answer"] = "Podaj pytanie dotyczące standardu ISO, procesu lub produktu."
        return append_audit(state, "input_blocked", reason=state["block_reason"])
    if contains_pii(query):
        state["blocked"] = True
        state["block_reason"] = "pii_detected"
        state["final_answer"] = (
            "Pytanie zawiera potencjalne dane osobowe lub wrażliwe. Usuń je i zapytaj ponownie."
        )
        return append_audit(state, "input_blocked", reason=state["block_reason"])
    lowered = query.lower()
    if any(keyword in lowered for keyword in INJECTION_KEYWORDS):
        state["blocked"] = True
        state["block_reason"] = "possible_prompt_injection"
        state["final_answer"] = (
            "Wykryto potencjalną próbę modyfikacji instrukcji systemu."
            " Zapytaj ponownie w standardowy sposób."
        )
        return append_audit(state, "input_blocked", reason=state["block_reason"])
    state["blocked"] = False
    return append_audit(state, "input_allowed")
