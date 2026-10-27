"""Agent node functions for the LangGraph workflow.

Pipeline (wiring lives in :mod:`iso_insight.pipeline.stages`):

    orchestrator -> iso_parser -> retrieval -> evidence
    -> gap_analysis -> impact_risk -> draft_answer -> reflector
    -> (improve -> retrieval loop, bounded by max_iterations)
    -> output_validator -> confidence_check -> final_answer | escalate

Notes:
- Input guardrails (PII / prompt injection) are NOT a node here — they
  run at the API boundary before the task is enqueued (see
  :mod:`iso_insight.pipeline.guardrails`).
- All LLM interaction goes through the injected :class:`LLMClient` port;
  the prompts themselves are versioned files in :mod:`iso_insight.llm.prompts`.
- Every LLM-backed node degrades gracefully: on a failed LLM call it
  records an "llm_error" audit event and falls back to deterministic
  outputs, so the workflow can still complete (usually ending in
  escalation) instead of crashing.
- The routing functions read the state and return the next node name.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from iso_insight.config import Settings
from iso_insight.llm.prompts import load_prompt
from iso_insight.ports.llm import LLMClient
from iso_insight.ports.tools import ToolGateway
from iso_insight.ports.tracing import Tracing
from iso_insight.retrieval.search import (
    confluence_search,
    jira_search,
    rag_search,
    select_rag_servers,
)
from iso_insight.schemas.workflow import SourceRef, WorkflowState, append_audit

SearchFunc = Callable[..., Awaitable[list[SourceRef]]]


def _clean_list(value: Any) -> list[str]:
    """Coerce an LLM value (list, string or None) into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _limited_sources(state: WorkflowState, limit: int = 5) -> list[SourceRef]:
    """First N sources — keeps LLM payloads small and bounded."""
    return state.get("sources", [])[:limit]


def _current_query(state: WorkflowState) -> str:
    """Query used for retrieval (the refined one when the improve node has run)."""
    return state.get("retrieval_query") or state.get("original_query") or state.get("query", "")


def _original_query(state: WorkflowState) -> str:
    """The user's question for the current run."""
    return state.get("original_query") or state.get("query", "")


def _infer_standards(text: str) -> list[str]:
    """Heuristic ISO-standard detection from the query text.

    Fallback used when the LLM parser is unavailable. Matching is done on
    the space-stripped text so both "ISO 8583" and "ISO8583" hit, plus a
    few message-family keywords (pacs/camt -> 20022, emv -> 8583).
    """
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    standards: list[str] = []
    if "iso20022" in compact or "pacs" in lowered or "camt" in lowered:
        standards.append("ISO20022")
    if "iso8583" in compact or "emv" in lowered:
        standards.append("ISO8583")
    if "iso15022" in compact:
        standards.append("ISO15022")
    return standards


def _llm_payload(payload: dict[str, Any]) -> str:
    """Serialise a node payload for the LLM port (compact, non-ASCII kept)."""
    return json.dumps(payload, ensure_ascii=False)


async def _safe_search(
    search_func: SearchFunc,
    state: WorkflowState,
    tool_name: str,
    *args: Any,
) -> list[SourceRef]:
    """Run one MCP search, converting any failure into an audit event (empty result).

    Only JSON-safe values (tool name, args, error text) go into the audit
    log — it is returned to the API caller and stored in Celery results.
    """
    try:
        return await search_func(state, *args)
    except Exception as exc:
        append_audit(state, "mcp_tool_error", tool=tool_name, args=list(args), error=str(exc))
        return []


class NodeFunctions:
    """The 13 workflow nodes + 2 routers, with all ports injected.

    Each public node is the traced version of a ``_``-prefixed private
    method (the tracing span is attached here — the domain never imports
    tracing infrastructure; the span name matches the legacy
    ``agent.<node>`` Langfuse names).
    """

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        tools: ToolGateway,
        tracing: Tracing,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._tools = tools
        self._tracing = tracing
        observe = tracing.observe
        self.orchestrator = observe(name="agent.orchestrator")(self._orchestrator)
        self.iso_parser = observe(name="agent.iso_parser")(self._iso_parser)
        self.retrieval = observe(name="agent.retrieval")(self._retrieval)
        self.evidence_validator = observe(name="agent.evidence")(self._evidence_validator)
        self.gap_analysis = observe(name="agent.gap_analysis")(self._gap_analysis)
        self.impact_risk = observe(name="agent.impact_risk")(self._impact_risk)
        self.draft_answer = observe(name="agent.draft_answer")(self._draft_answer)
        self.reflector = observe(name="agent.reflector")(self._reflector)
        self.improve = observe(name="agent.improve")(self._improve)
        self.output_validator = observe(name="agent.output")(self._output_validator)
        self.confidence_check = observe(name="agent.confidence")(self._confidence_check)
        self.final_answer = observe(name="agent.final_answer")(self._final_answer)
        self.escalate = observe(name="agent.escalate")(self._escalate)

    # ── routing ──────────────────────────────────────────────────────────

    def route_after_reflector(self, state: WorkflowState) -> str:
        """Critic decision: pass (or budget spent) -> output; fail -> improve loop."""
        if int(state.get("iteration", 0)) >= self._settings.max_iterations:
            return "output"
        if state.get("critic_verdict") == "pass":
            return "output"
        return "improve"

    def route_after_confidence(self, state: WorkflowState) -> str:
        """Confidence gate: above threshold -> final answer, below -> escalate."""
        confidence = float(state.get("confidence", 0.0))
        return "final" if confidence >= self._settings.low_confidence_threshold else "escalate"

    # ── nodes ────────────────────────────────────────────────────────────

    def _orchestrator(self, state: WorkflowState) -> WorkflowState:
        """Start a new run: reset per-run state from the current query.

        With a checkpointer, a new request on an existing thread inherits
        the previous run's state. Per-run fields must be reset here,
        otherwise the second query in the same thread_id would reuse the
        stale original_query/retrieval_query, iteration (disabling the
        improve loop) and the old sources/drafts. The audit log is
        intentionally kept — it is the thread-level audit trail.
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

    async def _iso_parser(self, state: WorkflowState) -> WorkflowState:
        """Detect the ISO standard and extract requirements (LLM).

        Falls back to keyword heuristics (_infer_standards) when the LLM
        call fails or returns nothing usable.
        """
        payload = _llm_payload(
            {
                "original_query": _original_query(state),
                "query": _current_query(state),
            }
        )
        try:
            result = await self._llm.json(load_prompt("iso_parser").text, payload)
            llm_used = True
        except Exception as exc:
            append_audit(state, "llm_error", agent="iso_parser", error=str(exc))
            result = {"standards": [], "requirements": []}
            llm_used = False

        standards = _clean_list(
            result.get("standards") or result.get("standard") or result.get("items")
        )

        # Normalize to uppercase so the RAG_STANDARD_MAP lookup is stable.
        standards = [str(item).strip().upper() for item in standards if str(item).strip()]

        if not standards:
            standards = _infer_standards(_current_query(state))

        requirements = _clean_list(result.get("requirements") or result.get("items"))

        if not requirements:
            query_lower = _current_query(state).lower()
            if "iso" in query_lower or "standard" in query_lower:
                requirements.append("Wymagania standardu ISO wymagają identyfikacji i weryfikacji.")

        state["detected_standards"] = standards
        state["iso_requirements"] = requirements

        return append_audit(
            state,
            "iso_parser_completed",
            detected_standards=standards,
            requirement_count=len(requirements),
            llm_used=llm_used,
        )

    async def _rag_search(self, state: WorkflowState, server_name: str) -> list[SourceRef]:
        return await rag_search(state, self._tools, self._settings, server_name)

    async def _jira_search(self, state: WorkflowState) -> list[SourceRef]:
        return await jira_search(state, self._tools, self._settings)

    async def _confluence_search(self, state: WorkflowState) -> list[SourceRef]:
        return await confluence_search(state, self._tools, self._settings)

    async def _retrieval(self, state: WorkflowState) -> WorkflowState:
        """Collect evidence for the current query.

        Queries the ISO-specific RAG bases (routed by detected standard)
        plus the default RAG base, Jira and Confluence; deduplicates by
        (source_type, source_id) and stores quotes as the rag_context.
        Runs once per critic-loop iteration with the (possibly refined)
        query.
        """
        standards = state.get("detected_standards", [])

        # Default base first, then per-ISO bases (filtered to configured servers).
        server_names = select_rag_servers(standards, self._settings)
        state["selected_rag_servers"] = server_names

        rag_sources: list[SourceRef] = []
        for server_name in server_names:
            # First server_name feeds the audit "tool" field, the second is
            # the positional argument to the search function.
            server_sources = await _safe_search(self._rag_search, state, server_name, server_name)
            rag_sources.extend(server_sources)

        jira_sources = await _safe_search(self._jira_search, state, "jira")
        confluence_sources = await _safe_search(self._confluence_search, state, "confluence")

        sources: list[SourceRef] = []
        sources.extend(rag_sources)
        sources.extend(jira_sources)
        sources.extend(confluence_sources)

        # Deduplicate while preserving the first-seen order.
        unique_sources: list[SourceRef] = []
        seen: set[tuple[Any, Any]] = set()
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

    def _evidence_validator(self, state: WorkflowState) -> WorkflowState:
        """Keep only sources that carry a real quote and derive a base confidence.

        Confidence scales with the number of usable sources (0.55 + 0.1
        each, capped at 0.95); with no sources it drops to 0.25 so the run
        is very likely to end in escalation rather than an ungrounded
        answer.
        """
        valid_sources = [source for source in state.get("sources", []) if source.get("quote")]
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

    async def _gap_analysis(self, state: WorkflowState) -> WorkflowState:
        """Compare ISO requirements with the retrieved context and list gaps.

        LLM findings are merged with deterministic gaps (missing
        requirements or missing sources) so the gap list is never
        silently empty.
        """
        payload = _llm_payload(
            {
                "original_query": _original_query(state),
                "query": _current_query(state),
                "detected_standards": state.get("detected_standards", []),
                "iso_requirements": state.get("iso_requirements", []),
                "sources": _limited_sources(state),
                "rag_context": state.get("rag_context", [])[:5],
            }
        )
        try:
            result = await self._llm.json(load_prompt("gap_analysis").text, payload)
            llm_used = True
        except Exception as exc:
            append_audit(state, "llm_error", agent="gap_analysis", error=str(exc))
            result = {"gaps": []}
            llm_used = False

        llm_gaps = _clean_list(result.get("gaps") or result.get("items"))

        deterministic_gaps: list[str] = []
        if not state.get("iso_requirements"):
            deterministic_gaps.append("Brak zidentyfikowanych wymagań ISO.")
        if not state.get("sources"):
            deterministic_gaps.append("Brak źródeł potwierdzających odpowiedź.")

        # Deduplicate, deterministic findings first.
        gaps = list(dict.fromkeys(deterministic_gaps + llm_gaps))
        state["gaps"] = gaps

        return append_audit(state, "gap_analysis_completed", gap_count=len(gaps), llm_used=llm_used)

    async def _impact_risk(self, state: WorkflowState) -> WorkflowState:
        """Estimate business impact, risks and recommendations (LLM).

        Merges LLM output with deterministic observations (requirements in
        scope, gaps found, missing sources) so the section is always
        populated with something grounded.
        """
        payload = _llm_payload(
            {
                "original_query": _original_query(state),
                "query": _current_query(state),
                "detected_standards": state.get("detected_standards", []),
                "iso_requirements": state.get("iso_requirements", []),
                "gaps": state.get("gaps", []),
                "sources": _limited_sources(state),
            }
        )
        try:
            result = await self._llm.json(load_prompt("impact_risk").text, payload)
            llm_used = True
        except Exception as exc:
            append_audit(state, "llm_error", agent="impact_risk", error=str(exc))
            result = {}
            llm_used = False

        llm_impact = _clean_list(result.get("impact") or result.get("items"))
        llm_risks = _clean_list(result.get("risks"))
        recommendations = _clean_list(result.get("recommendations"))

        deterministic_impact: list[str] = []
        if state.get("iso_requirements"):
            deterministic_impact.append("Wpływ na procesy objęte standardem ISO.")
        if state.get("gaps"):
            deterministic_impact.append("Wymagana weryfikacja przez analityka biznesowego.")
        if not state.get("sources"):
            deterministic_impact.append("Ryzyko odpowiedzi bez wystarczających źródeł.")

        impact = list(
            dict.fromkeys(deterministic_impact + llm_impact + llm_risks + recommendations)
        )
        state["impact"] = impact

        return append_audit(
            state, "impact_risk_completed", impact_count=len(impact), llm_used=llm_used
        )

    async def _draft_answer(self, state: WorkflowState) -> WorkflowState:
        """Generate the draft answer strictly from sources and requirements (LLM).

        On an LLM failure a short fallback message is drafted (marked with
        a caveat) so the pipeline can still reach the critic and the
        confidence gate.
        """
        payload = _llm_payload(
            {
                "original_query": _original_query(state),
                "query": _current_query(state),
                "detected_standards": state.get("detected_standards", []),
                "iso_requirements": state.get("iso_requirements", []),
                "gaps": state.get("gaps", []),
                "impact": state.get("impact", []),
                "sources": _limited_sources(state),
            }
        )
        try:
            result = await self._llm.json(load_prompt("draft_answer").text, payload)
            llm_used = True
        except Exception as exc:
            append_audit(state, "llm_error", agent="draft_answer", error=str(exc))
            result = {
                "answer": "Nie udało się wygenerować odpowiedzi przez LLM.",
                "caveats": ["Błąd wywołania LLM."],
            }
            llm_used = False

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
            llm_used=llm_used,
        )

    async def _reflector(self, state: WorkflowState) -> WorkflowState:
        """Critic pass: judge the draft (pass/fail) and bump the iteration counter.

        Short-circuits to "fail" (no LLM call) when there is no draft or
        no sources — such an answer can never pass. The verdict and
        feedback drive the routing decision (output vs improve) in
        route_after_reflector.
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

        payload = _llm_payload(
            {
                "original_query": _original_query(state),
                "query": _current_query(state),
                "detected_standards": state.get("detected_standards", []),
                "draft_answer": draft,
                "sources": _limited_sources(state),
                "gaps": state.get("gaps", []),
                "impact": state.get("impact", []),
                "caveats": state.get("draft_caveats", []),
            }
        )
        try:
            result = await self._llm.json(load_prompt("reflector").text, payload)
            llm_used = True
        except Exception as exc:
            append_audit(state, "llm_error", agent="reflector", error=str(exc))
            result = {
                "verdict": "fail",
                "feedback": "Błąd wywołania LLM w reflectorze.",
            }
            llm_used = False

        # Anything that is not an explicit "pass" is treated as a fail.
        raw_verdict = str(result.get("verdict", "fail")).strip().lower()
        verdict: Literal["pass", "fail"] = "pass" if raw_verdict == "pass" else "fail"

        state["critic_verdict"] = verdict
        state["critic_feedback"] = str(result.get("feedback", "")).strip()

        return append_audit(
            state,
            "reflector_completed",
            verdict=verdict,
            feedback=state["critic_feedback"],
            iteration=state["iteration"],
            llm_used=llm_used,
        )

    async def _improve(self, state: WorkflowState) -> WorkflowState:
        """Refine the retrieval query based on critic feedback (LLM).

        Feeds back into the retrieval node; the loop is bounded by
        ``settings.max_iterations`` in route_after_reflector.
        """
        payload = _llm_payload(
            {
                "original_query": _original_query(state),
                "current_retrieval_query": _current_query(state),
                "detected_standards": state.get("detected_standards", []),
                "critic_feedback": state.get("critic_feedback", ""),
                "gaps": state.get("gaps", []),
                "impact": state.get("impact", []),
                "sources": _limited_sources(state),
            }
        )
        try:
            result = await self._llm.json(load_prompt("improve").text, payload)
            llm_used = True
        except Exception as exc:
            append_audit(state, "llm_error", agent="improve", error=str(exc))
            result = {}
            llm_used = False

        # Keep the previous query when the LLM produced nothing usable.
        refined_query = str(result.get("refined_query", "")).strip()
        state["retrieval_query"] = (
            refined_query or state.get("retrieval_query") or _original_query(state)
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
            llm_used=llm_used,
        )

    def _output_validator(self, state: WorkflowState) -> WorkflowState:
        """Assemble the final answer from the draft, sources and caveats.

        Guarantees a user-facing message in every case:
        - no draft   -> "draft_missing"
        - no sources -> explicit "missing_sources" notice, capped confidence
        - otherwise  -> draft + source list + caveats, audit_status "ok"
        """
        draft = state.get("draft_answer", "")
        sources = state.get("sources", [])
        caveats = state.get("draft_caveats", [])

        if not draft or not draft.strip():
            state["final_answer"] = "Nie udało się wygenerować odpowiedzi."
            state["audit_status"] = "draft_missing"
            state["confidence"] = 0.0
            return append_audit(state, "output_validated", audit_status=state["audit_status"])

        if not sources:
            state["final_answer"] = (
                "Nie mogę potwierdzić odpowiedzi bez wystarczających źródeł. "
                "Proszę doprecyzować: standard, wersję, produkt, kanał, proces "
                "lub właściciela dokumentu."
            )
            state["audit_status"] = "missing_sources"
            state["confidence"] = min(float(state.get("confidence", 0.0)), 0.2)
            return append_audit(state, "output_validated", audit_status=state["audit_status"])

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

    def _confidence_check(self, state: WorkflowState) -> WorkflowState:
        """Tag the audit status based on the confidence threshold.

        High confidence keeps whatever status the pipeline set (e.g.
        "ok"); low confidence overwrites it with "low_confidence" — the
        routing function then sends the run to escalation.
        """
        confidence = float(state.get("confidence", 0.0))

        if confidence >= self._settings.low_confidence_threshold:
            state["audit_status"] = state.get("audit_status") or "high_confidence"
        else:
            state["audit_status"] = "low_confidence"

        return append_audit(state, "confidence_checked", confidence=confidence)

    def _final_answer(self, state: WorkflowState) -> WorkflowState:
        """Last-mile defaults before END: ensure final_answer/audit_status exist."""
        state.setdefault("final_answer", "Brak odpowiedzi.")
        state["audit_status"] = state.get("audit_status") or "completed"
        return append_audit(state, "workflow_completed")

    def _escalate(self, state: WorkflowState) -> WorkflowState:
        """Replace the answer with an escalation message (low-confidence run)."""
        state["final_answer"] = (
            "Niska pewność odpowiedzi. "
            "Proszę doprecyzować: standard, wersję, produkt, kanał, proces "
            "lub właściciela dokumentu. "
            "Alternatywnie eskaluj do analityka właściciela."
        )
        state["audit_status"] = "escalation_required"
        return append_audit(state, "workflow_escalated")
