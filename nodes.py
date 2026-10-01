"""LangGraph agent nodes and routing functions.

Pipeline (see graph.py for the wiring):

    orchestrator -> iso_parser -> retrieval -> evidence
    -> gap_analysis -> impact_risk -> draft_answer -> reflector
    -> (improve -> retrieval loop, bounded by MAX_ITERATIONS)
    -> output_validator -> confidence_check -> final_answer | escalate

Notes:
- Input guardrails (PII / prompt injection) are NOT a node here — they
  run at the API boundary before the task is enqueued (see guardrails.py
  and submit_query in iso_multiagent_api.py).
- The LLM system prompts below are intentionally kept in Polish — they
  are product text for the target analysts, not code documentation.
- Every LLM-backed node degrades gracefully: on a failed LLM call it
  records an "llm_error" audit event and falls back to deterministic
  outputs, so the workflow can still complete (usually ending in
  escalation) instead of crashing.
- The routing functions read the state and return the next node name.
"""

from __future__ import annotations

from typing import Any, List, Callable, Awaitable

from config import (
    MAX_ITERATIONS,
    LOW_CONFIDENCE_THRESHOLD,
    MCP_SERVERS,
    DEFAULT_RAG_SERVER,
    RAG_STANDARD_MAP,
)
from state import WorkflowState, SourceRef, append_audit
from tools import rag_search, jira_search, confluence_search
from llm import llm_json
from tracing import observe as langfuse_observe


# System prompt for the ISO parser node: detect the ISO standard
# (ISO20022 / ISO8583 / ...), the message type and the requirements that
# can be derived from the question; must answer with the strict JSON
# object shown at the end.
ISO_PARSER_SYSTEM = """
Jesteś agentem analitycznym do standardów ISO w banku.

Twoje zadanie:
1. Wyodrębnij z pytania wymagania ISO.
2. Zidentyfikuj standard, na przykład ISO20022, ISO8583, ISO15022.
3. Zidentyfikuj typ wiadomości lub obszar, jeśli jest podany.
4. Nie wymyślaj wymagań, których nie da się wyprowadzić z pytania.
5. Jeśli pytanie jest ogólne, zwróć puste listy.

Zwróć JSON w formacie:
{
  "standards": ["ISO20022"],
  "standard": "ISO20022",
  "message_type": "pacs.008.001.08",
  "requirements": ["wymaganie 1", "wymaganie 2"],
  "confidence": 0.0
}
"""


# System prompt for the gap analysis node: compare the ISO requirements
# with the retrieved RAG / Jira / Confluence context and list concrete
# gaps, missing information and open questions (no invented gaps).
GAP_ANALYSIS_SYSTEM = """
Jesteś agentem do analizy luk compliance / ISO.

Twoje zadanie:
1. Porównaj wymagania ISO z kontekstem z RAG / Jira / Confluence.
2. Wskaż luki, brakujące informacje i miejsca wymagające decyzji.
3. Nie twórz luk bez podstawy.
4. Jeśli brak źródeł, powiedz o tym wprost.

Zwróć JSON w formacie:
{
  "gaps": ["luka 1", "luka 2"],
  "questions": ["pytanie 1"],
  "confidence": 0.0
}
"""


# System prompt for the impact / risk node: assess the business impact of
# ISO changes on processes, systems and integrations, list operational /
# compliance / rollout risks, and give recommendations for the analyst.
IMPACT_RISK_SYSTEM = """
Jesteś agentem do oceny wpływu i ryzyka w banku.

Twoje zadanie:
1. Oceń wpływ zmian ISO na procesy, systemy i integracje.
2. Wskaż ryzyka operacyjne, compliance i wdrożeniowe.
3. Podaj rekomendacje dla analityka biznesowego.
4. Nie wymyślaj ryzyk bez związku z danymi.

Zwróć JSON w formacie:
{
  "impact": ["wpływ 1"],
  "risks": ["ryzyko 1"],
  "recommendations": ["rekomendacja 1"],
  "confidence": 0.0
}
"""


# System prompt for the draft answer node: answer the analyst's question
# strictly from the provided sources and requirements, state uncertainty
# explicitly, never invent ISO requirements, never create tickets.
DRAFT_ANSWER_SYSTEM = """
Jesteś agentem generującym odpowiedź dla analityka biznesowego w banku.

Twoje zadanie:
1. Odpowiedz na pytanie użytkownika.
2. Opieraj się wyłącznie na podanych źródłach i wymaganiach.
3. Jeśli źródła są niewystarczające, powiedz wprost.
4. Podaj niepewności.
5. Nie twórz ticketów.
6. Nie wymyślaj wymagań ISO.

Zwróć JSON w formacie:
{
  "answer": "finalny tekst odpowiedzi",
  "caveats": ["zastrzeżenie 1"],
  "confidence": 0.0
}
"""


# System prompt for the reflector (critic) node: judge the draft answer
# as pass / fail, check it against the question and the sources, and give
# concrete feedback on what is missing or inconsistent.
REFLECTOR_SYSTEM = """
Jesteś agentem walidującym odpowiedź w systemie compliance / ISO.

Twoje zadanie:
1. Sprawdź, czy odpowiedź odpowiada na pytanie.
2. Sprawdź, czy opiera się na źródłach.
3. Sprawdź, czy nie ma sprzeczności.
4. Sprawdź, czy nie brakuje wymagań ISO.
5. Ocena: pass albo fail.

Zwróć JSON w formacie:
{
  "verdict": "pass" | "fail",
  "feedback": "konkretny feedback",
  "missing": ["brakujący element 1"],
  "confidence": 0.0
}
"""


# System prompt for the improve node: rewrite the retrieval query for the
# next critic-loop iteration based on the reflector's feedback, keeping
# the ISO / banking context and the intent of the original question.
IMPROVE_SYSTEM = """
Jesteś agentem poprawiającym zapytanie do bazy wiedzy.

Twoje zadanie:
1. Na podstawie feedbacku z reflectora sformułuj lepsze zapytanie do RAG / Jira / Confluence.
2. Utrzymaj kontekst ISO / bankowości.
3. Nie zmieniaj sensu oryginalnego pytania.
4. Zwróć tylko poprawne zapytanie i krótki feedback.

Zwróć JSON w formacie:
{
  "refined_query": "poprawione zapytanie",
  "search_hints": ["hint 1"],
  "feedback": "krótki feedback"
}
"""


def _clean_list(value: Any) -> List[str]:
    """Coerce an LLM value (list, string or None) into a list of non-empty strings."""
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    text = str(value).strip()
    return [text] if text else []


def _limited_sources(state: WorkflowState, limit: int = 5) -> List[SourceRef]:
    """First N sources — keeps LLM payloads small and bounded."""
    return state.get("sources", [])[:limit]


def _current_query(state: WorkflowState) -> str:
    """Query used for retrieval (the refined one when the improve node has run)."""
    return (
        state.get("retrieval_query")
        or state.get("original_query")
        or state.get("query", "")
    )


def _original_query(state: WorkflowState) -> str:
    """The user's question for the current run."""
    return state.get("original_query") or state.get("query", "")


def _infer_standards(text: str) -> List[str]:
    """Heuristic ISO-standard detection from the query text.

    Fallback used when the LLM parser is unavailable. Matching is done on
    the space-stripped text so both "ISO 8583" and "ISO8583" hit, plus a
    few message-family keywords (pacs/camt -> 20022, emv -> 8583).
    """
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    standards: List[str] = []

    if "iso20022" in compact or "pacs" in lowered or "camt" in lowered:
        standards.append("ISO20022")

    if "iso8583" in compact or "emv" in lowered:
        standards.append("ISO8583")

    if "iso15022" in compact:
        standards.append("ISO15022")

    return standards


@langfuse_observe(name="agent.orchestrator")
def orchestrator(state: WorkflowState) -> WorkflowState:
    """Start a new run: reset per-run state from the current query.

    With a checkpointer, a new request on an existing thread inherits the
    previous run's state. Per-run fields must be reset here, otherwise the
    second query in the same thread_id would reuse the stale
    original_query/retrieval_query, iteration=MAX (disabling the improve
    loop) and the old sources/drafts. The audit log is intentionally
    kept — it is the thread-level audit trail.
    """
    current_query = state.get("query", "")

    state["original_query"] = current_query
    state["retrieval_query"] = current_query

    state["detected_standards"] = []
    state["selected_rag_servers"] = []
    state["iso_requirements"] = []
    state["rag_context"] = []
    state["sources"] = []
    state["gaps"] = []
    state["impact"] = []
    state["draft_answer"] = ""
    state["draft_caveats"] = []
    state["critic_verdict"] = None
    state["critic_feedback"] = ""
    state["confidence"] = 0.0
    state["iteration"] = 0
    state["final_answer"] = ""
    state["audit_status"] = ""

    state.setdefault("audit_log", [])

    return append_audit(
        state,
        "orchestrator_planned",
        original_query=state["original_query"],
        retrieval_query=state["retrieval_query"],
    )


@langfuse_observe(name="agent.iso_parser")
async def iso_parser(state: WorkflowState) -> WorkflowState:
    """Detect the ISO standard and extract requirements (LLM).

    Falls back to keyword heuristics (_infer_standards) when the LLM call
    fails or returns nothing usable.
    """
    payload = {
        "original_query": _original_query(state),
        "query": _current_query(state),
    }

    try:
        result = await llm_json(ISO_PARSER_SYSTEM, payload)
    except Exception as exc:
        state.setdefault("audit_log", []).append(
            {
                "event": "llm_error",
                "agent": "iso_parser",
                "error": str(exc),
            }
        )
        result = {
            "standards": [],
            "requirements": [],
        }

    standards = _clean_list(
        result.get("standards")
        or result.get("standard")
        or result.get("items")
    )

    # Normalize to uppercase so the RAG_STANDARD_MAP lookup is stable.
    standards = [
        str(item).strip().upper()
        for item in standards
        if str(item).strip()
    ]

    if not standards:
        standards = _infer_standards(_current_query(state))

    requirements = _clean_list(
        result.get("requirements")
        or result.get("items")
    )

    if not requirements:
        query_lower = _current_query(state).lower()
        if "iso" in query_lower or "standard" in query_lower:
            requirements.append(
                "Wymagania standardu ISO wymagają identyfikacji i weryfikacji."
            )

    state["detected_standards"] = standards
    state["iso_requirements"] = requirements

    return append_audit(
        state,
        "iso_parser_completed",
        detected_standards=standards,
        requirement_count=len(requirements),
        llm_used=True,
    )


async def _safe_search(
    search_func: Callable[..., Awaitable[List[SourceRef]]],
    state: WorkflowState,
    tool_name: str,
    *args: Any,
) -> List[SourceRef]:
    """Run one MCP search, converting any failure into an audit event (empty result)."""
    try:
        return await search_func(state, *args)
    except Exception as exc:
        state.setdefault("audit_log", []).append(
            {
                "event": "mcp_tool_error",
                "tool": tool_name,
                "args": list(args),
                "error": str(exc),
            }
        )
        return []


@langfuse_observe(name="agent.retrieval")
async def retrieval(state: WorkflowState) -> WorkflowState:
    """Collect evidence for the current query.

    Queries the ISO-specific RAG bases (routed by detected standard) plus
    the default RAG base, Jira and Confluence; deduplicates by
    (source_type, source_id) and stores quotes as the rag_context.
    Runs once per critic-loop iteration with the (possibly refined) query.
    """
    standards = state.get("detected_standards", [])

    # Start from the default base, then add per-ISO bases when mapped.
    server_names: List[str] = [DEFAULT_RAG_SERVER]

    for standard in standards:
        standard_key = str(standard).strip().upper()
        rag_server = RAG_STANDARD_MAP.get(standard_key)

        if (
            rag_server
            and rag_server in MCP_SERVERS
            and rag_server not in server_names
        ):
            server_names.append(rag_server)

    state["selected_rag_servers"] = server_names

    rag_sources: List[SourceRef] = []

    for server_name in server_names:
        sources = await _safe_search(
            rag_search,
            state,
            server_name,
            server_name,
        )
        rag_sources.extend(sources)

    jira_sources = await _safe_search(jira_search, state, "jira")
    confluence_sources = await _safe_search(confluence_search, state, "confluence")

    sources: List[SourceRef] = []
    sources.extend(rag_sources)
    sources.extend(jira_sources)
    sources.extend(confluence_sources)

    # Deduplicate while preserving the first-seen order.
    unique_sources: List[SourceRef] = []
    seen = set()

    for source in sources:
        key = (source.get("source_type"), source.get("source_id"))
        if key in seen:
            continue
        seen.add(key)
        unique_sources.append(source)

    state["sources"] = unique_sources
    state["rag_context"] = [source.get("quote", "") for source in unique_sources]

    return append_audit(
        state,
        "retrieval_completed",
        detected_standards=standards,
        selected_rag_servers=server_names,
        rag_count=len(rag_sources),
        jira_count=len(jira_sources),
        confluence_count=len(confluence_sources),
        total_source_count=len(unique_sources),
    )


@langfuse_observe(name="agent.evidence")
def evidence_validator(state: WorkflowState) -> WorkflowState:
    """Keep only sources that carry a real quote and derive a base confidence.

    Confidence scales with the number of usable sources (0.55 + 0.1 each,
    capped at 0.95); with no sources it drops to 0.25 so the run is very
    likely to end in escalation rather than an ungrounded answer.
    """
    valid_sources = [
        source for source in state.get("sources", [])
        if source.get("quote")
    ]

    state["sources"] = valid_sources

    if valid_sources:
        state["confidence"] = min(0.95, 0.55 + 0.1 * len(valid_sources))
    else:
        state["confidence"] = 0.25

    return append_audit(
        state,
        "evidence_validated",
        source_count=len(valid_sources),
        confidence=state["confidence"],
    )


@langfuse_observe(name="agent.gap_analysis")
async def gap_analysis(state: WorkflowState) -> WorkflowState:
    """Compare ISO requirements with the retrieved context and list gaps.

    LLM findings are merged with deterministic gaps (missing requirements
    or missing sources) so the gap list is never silently empty.
    """
    payload = {
        "original_query": _original_query(state),
        "query": _current_query(state),
        "detected_standards": state.get("detected_standards", []),
        "iso_requirements": state.get("iso_requirements", []),
        "sources": _limited_sources(state),
        "rag_context": state.get("rag_context", [])[:5],
    }

    try:
        result = await llm_json(GAP_ANALYSIS_SYSTEM, payload)
    except Exception as exc:
        state.setdefault("audit_log", []).append(
            {
                "event": "llm_error",
                "agent": "gap_analysis",
                "error": str(exc),
            }
        )
        result = {"gaps": []}

    llm_gaps = _clean_list(result.get("gaps") or result.get("items"))

    deterministic_gaps = []
    if not state.get("iso_requirements"):
        deterministic_gaps.append("Brak zidentyfikowanych wymagań ISO.")

    if not state.get("sources"):
        deterministic_gaps.append("Brak źródeł potwierdzających odpowiedź.")

    # Deduplicate, deterministic findings first.
    gaps = list(dict.fromkeys(deterministic_gaps + llm_gaps))
    state["gaps"] = gaps

    return append_audit(
        state,
        "gap_analysis_completed",
        gap_count=len(gaps),
        llm_used=True,
    )


@langfuse_observe(name="agent.impact_risk")
async def impact_risk(state: WorkflowState) -> WorkflowState:
    """Estimate business impact, risks and recommendations (LLM).

    Merges LLM output with deterministic observations (requirements in
    scope, gaps found, missing sources) so the section is always populated
    with something grounded.
    """
    payload = {
        "original_query": _original_query(state),
        "query": _current_query(state),
        "detected_standards": state.get("detected_standards", []),
        "iso_requirements": state.get("iso_requirements", []),
        "gaps": state.get("gaps", []),
        "sources": _limited_sources(state),
    }

    try:
        result = await llm_json(IMPACT_RISK_SYSTEM, payload)
    except Exception as exc:
        state.setdefault("audit_log", []).append(
            {
                "event": "llm_error",
                "agent": "impact_risk",
                "error": str(exc),
            }
        )
        result = {}

    llm_impact = _clean_list(result.get("impact") or result.get("items"))
    llm_risks = _clean_list(result.get("risks"))
    recommendations = _clean_list(result.get("recommendations"))

    deterministic_impact = []
    if state.get("iso_requirements"):
        deterministic_impact.append("Wpływ na procesy objęte standardem ISO.")

    if state.get("gaps"):
        deterministic_impact.append("Wymagana weryfikacja przez analityka biznesowego.")

    if not state.get("sources"):
        deterministic_impact.append("Ryzyko odpowiedzi bez wystarczających źródeł.")

    impact = list(
        dict.fromkeys(
            deterministic_impact + llm_impact + llm_risks + recommendations
        )
    )

    state["impact"] = impact

    return append_audit(
        state,
        "impact_risk_completed",
        impact_count=len(impact),
        llm_used=True,
    )


@langfuse_observe(name="agent.draft_answer")
async def draft_answer(state: WorkflowState) -> WorkflowState:
    """Generate the draft answer strictly from sources and requirements (LLM).

    On an LLM failure a short fallback message is drafted (marked with a
    caveat) so the pipeline can still reach the critic and the confidence
    gate.
    """
    payload = {
        "original_query": _original_query(state),
        "query": _current_query(state),
        "detected_standards": state.get("detected_standards", []),
        "iso_requirements": state.get("iso_requirements", []),
        "gaps": state.get("gaps", []),
        "impact": state.get("impact", []),
        "sources": _limited_sources(state),
    }

    try:
        result = await llm_json(DRAFT_ANSWER_SYSTEM, payload)
    except Exception as exc:
        state.setdefault("audit_log", []).append(
            {
                "event": "llm_error",
                "agent": "draft_answer",
                "error": str(exc),
            }
        )
        result = {
            "answer": "Nie udało się wygenerować odpowiedzi przez LLM.",
            "caveats": ["Błąd wywołania LLM."],
        }

    answer = str(result.get("answer", "")).strip()

    if not answer and result.get("items"):
        answer = " ".join(_clean_list(result.get("items")))

    state["draft_answer"] = answer
    state["draft_caveats"] = _clean_list(result.get("caveats"))

    return append_audit(
        state,
        "draft_generated",
        answer_length=len(state["draft_answer"]),
        caveat_count=len(state["draft_caveats"]),
        llm_used=True,
    )


@langfuse_observe(name="agent.reflector")
async def reflector(state: WorkflowState) -> WorkflowState:
    """Critic pass: judge the draft (pass/fail) and bump the iteration counter.

    Short-circuits to "fail" (no LLM call) when there is no draft or no
    sources — such an answer can never pass. The verdict and feedback drive
    the routing decision (output vs improve) in route_after_reflector.
    """
    state["iteration"] = int(state.get("iteration", 0)) + 1

    draft = state.get("draft_answer", "")
    sources = state.get("sources", [])

    if not draft or not draft.strip():
        state["critic_verdict"] = "fail"
        state["critic_feedback"] = "Brak draftu odpowiedzi."
        return append_audit(
            state,
            "reflector_completed",
            verdict="fail",
            iteration=state["iteration"],
            llm_used=False,
        )

    if not sources:
        state["critic_verdict"] = "fail"
        state["critic_feedback"] = "Odpowiedź nie ma wystarczających źródeł."
        return append_audit(
            state,
            "reflector_completed",
            verdict="fail",
            iteration=state["iteration"],
            llm_used=False,
        )

    payload = {
        "original_query": _original_query(state),
        "query": _current_query(state),
        "detected_standards": state.get("detected_standards", []),
        "draft_answer": draft,
        "sources": _limited_sources(state),
        "gaps": state.get("gaps", []),
        "impact": state.get("impact", []),
        "caveats": state.get("draft_caveats", []),
    }

    try:
        result = await llm_json(REFLECTOR_SYSTEM, payload)
    except Exception as exc:
        state.setdefault("audit_log", []).append(
            {
                "event": "llm_error",
                "agent": "reflector",
                "error": str(exc),
            }
        )
        result = {
            "verdict": "fail",
            "feedback": "Błąd wywołania LLM w reflectorze.",
        }

    # Anything that is not an explicit "pass" is treated as a fail.
    verdict = str(result.get("verdict", "fail")).strip().lower()
    if verdict not in ("pass", "fail"):
        verdict = "fail"

    state["critic_verdict"] = verdict
    state["critic_feedback"] = str(result.get("feedback", "")).strip()

    return append_audit(
        state,
        "reflector_completed",
        verdict=verdict,
        feedback=state["critic_feedback"],
        iteration=state["iteration"],
        llm_used=True,
    )


@langfuse_observe(name="agent.improve")
async def improve(state: WorkflowState) -> WorkflowState:
    """Refine the retrieval query based on critic feedback (LLM).

    Feeds back into the retrieval node; the loop is bounded by
    MAX_ITERATIONS in route_after_reflector.
    """
    payload = {
        "original_query": _original_query(state),
        "current_retrieval_query": _current_query(state),
        "detected_standards": state.get("detected_standards", []),
        "critic_feedback": state.get("critic_feedback", ""),
        "gaps": state.get("gaps", []),
        "impact": state.get("impact", []),
        "sources": _limited_sources(state),
    }

    try:
        result = await llm_json(IMPROVE_SYSTEM, payload)
    except Exception as exc:
        state.setdefault("audit_log", []).append(
            {
                "event": "llm_error",
                "agent": "improve",
                "error": str(exc),
            }
        )
        result = {}

    # Keep the previous query when the LLM produced nothing usable.
    refined_query = str(result.get("refined_query", "")).strip()
    state["retrieval_query"] = (
        refined_query
        or state.get("retrieval_query")
        or _original_query(state)
    )

    state["critic_feedback"] = (
        str(result.get("feedback", "")).strip()
        or state.get("critic_feedback")
        or "Popraw odpowiedź i uzupełnij źródła."
    )

    return append_audit(
        state,
        "improve_completed",
        retrieval_query=state["retrieval_query"],
        llm_used=True,
    )


@langfuse_observe(name="agent.output")
def output_validator(state: WorkflowState) -> WorkflowState:
    """Assemble the final answer from the draft, sources and caveats.

    Guarantees a user-facing message in every case:
    - no draft            -> "draft_missing"
    - no sources          -> explicit "missing_sources" notice, capped confidence
    - otherwise           -> draft + source list + caveats, audit_status "ok"
    """
    draft = state.get("draft_answer", "")
    sources = state.get("sources", [])
    caveats = state.get("draft_caveats", [])

    if not draft or not draft.strip():
        state["final_answer"] = "Nie udało się wygenerować odpowiedzi."
        state["audit_status"] = "draft_missing"
        state["confidence"] = 0.0
        return append_audit(
            state,
            "output_validated",
            audit_status=state["audit_status"],
        )

    if not sources:
        state["final_answer"] = (
            "Nie mogę potwierdzić odpowiedzi bez wystarczających źródeł. "
            "Proszę doprecyzować: standard, wersję, produkt, kanał, proces "
            "lub właściciela dokumentu."
        )
        state["audit_status"] = "missing_sources"
        state["confidence"] = min(float(state.get("confidence", 0.0)), 0.2)
        return append_audit(
            state,
            "output_validated",
            audit_status=state["audit_status"],
        )

    source_lines = [
        f"- {source.get('title', 'Źródło')} "
        f"({source.get('source_type', 'unknown')}): "
        f"{source.get('quote', '')}"
        for source in sources[:5]
    ]

    final_answer = f"{draft.strip()}\n\nŹródła:\n" + "\n".join(source_lines)

    if caveats:
        caveat_lines = [f"- {item}" for item in caveats[:5]]
        final_answer += "\n\nZastrzeżenia:\n" + "\n".join(caveat_lines)

    state["final_answer"] = final_answer
    state["audit_status"] = "ok"

    return append_audit(
        state,
        "output_validated",
        audit_status=state["audit_status"],
        source_count=len(sources),
        caveat_count=len(caveats),
    )


@langfuse_observe(name="agent.confidence")
def confidence_check(state: WorkflowState) -> WorkflowState:
    """Tag the audit status based on the confidence threshold.

    High confidence keeps whatever status the pipeline set (e.g. "ok");
    low confidence overwrites it with "low_confidence" — the routing
    function then sends the run to escalation.
    """
    confidence = float(state.get("confidence", 0.0))

    if confidence >= LOW_CONFIDENCE_THRESHOLD:
        state["audit_status"] = state.get("audit_status") or "high_confidence"
    else:
        state["audit_status"] = "low_confidence"

    return append_audit(
        state,
        "confidence_checked",
        confidence=confidence,
    )


@langfuse_observe(name="agent.final_answer")
def final_answer(state: WorkflowState) -> WorkflowState:
    """Last-mile defaults before END: ensure final_answer/audit_status exist."""
    state.setdefault("final_answer", "Brak odpowiedzi.")
    state["audit_status"] = state.get("audit_status") or "completed"
    return append_audit(state, "workflow_completed")


@langfuse_observe(name="agent.escalate")
def escalate(state: WorkflowState) -> WorkflowState:
    """Replace the answer with an escalation message (low-confidence run)."""
    state["final_answer"] = (
        "Niska pewność odpowiedzi. "
        "Proszę doprecyzować: standard, wersję, produkt, kanał, proces "
        "lub właściciela dokumentu. "
        "Alternatywnie eskaluj do analityka właściciela."
    )
    state["audit_status"] = "escalation_required"
    return append_audit(state, "workflow_escalated")


def route_after_reflector(state: WorkflowState) -> str:
    """Critic decision: pass (or budget spent) -> output; fail -> improve loop."""
    if int(state.get("iteration", 0)) >= MAX_ITERATIONS:
        return "output"

    if state.get("critic_verdict") == "pass":
        return "output"

    return "improve"


def route_after_confidence(state: WorkflowState) -> str:
    """Confidence gate: above threshold -> final answer, below -> escalate."""
    confidence = float(state.get("confidence", 0.0))
    return "final" if confidence >= LOW_CONFIDENCE_THRESHOLD else "escalate"
