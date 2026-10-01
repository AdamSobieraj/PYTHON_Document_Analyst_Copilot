"""LangGraph workflow assembly.

build_graph() wires the agent nodes (see nodes.py for what each stage
does) into a StateGraph with conditional edges.

Input guardrails (PII / prompt injection) run at the API boundary,
before the task is enqueued (submit_query in iso_multiagent_api.py) —
the graph itself starts with the orchestrator:

    START -> orchestrator -> iso_parser -> retrieval -> evidence -> gap -> impact
    -> draft -> reflector -> (pass/budget? output : improve -> retrieval)
    output -> confidence -> (high? final_answer : escalate) -> END

compile_app() compiles the graph with an in-memory checkpointer so
multi-turn threads (thread_id) work; it falls back to a stateless
compile when no checkpointer is available.
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from state import WorkflowState
from nodes import (
    orchestrator,
    iso_parser,
    retrieval,
    evidence_validator,
    gap_analysis,
    impact_risk,
    draft_answer,
    reflector,
    improve,
    output_validator,
    confidence_check,
    final_answer,
    escalate,
    route_after_reflector,
    route_after_confidence,
)


def build_graph() -> StateGraph:
    """Wire all nodes and (conditional) edges into the workflow graph.

    Returns an uncompiled StateGraph over WorkflowState. The fixed edges
    form the main pipeline; the two conditional edges are the decision
    points — after the reflector (critic loop back to retrieval vs.
    output) and after the confidence check (final answer vs. escalation).
    See the module docstring for the full flow.
    """
    graph = StateGraph(WorkflowState)

    graph.add_node("orchestrator", orchestrator)
    graph.add_node("iso_parser", iso_parser)
    graph.add_node("retrieval", retrieval)
    graph.add_node("evidence", evidence_validator)
    graph.add_node("gap", gap_analysis)
    graph.add_node("impact", impact_risk)
    graph.add_node("draft", draft_answer)
    graph.add_node("reflector", reflector)
    graph.add_node("improve", improve)
    graph.add_node("output", output_validator)
    graph.add_node("confidence", confidence_check)
    graph.add_node("final_answer", final_answer)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "orchestrator")

    graph.add_edge("orchestrator", "iso_parser")
    graph.add_edge("iso_parser", "retrieval")
    graph.add_edge("retrieval", "evidence")
    graph.add_edge("evidence", "gap")
    graph.add_edge("gap", "impact")
    graph.add_edge("impact", "draft")
    graph.add_edge("draft", "reflector")

    # Critic loop: a failed draft goes through "improve" and back to
    # retrieval; a passed one (or a spent iteration budget) goes to output.
    graph.add_conditional_edges(
        "reflector",
        route_after_reflector,
        {
            "output": "output",
            "improve": "improve",
        },
    )

    graph.add_edge("improve", "retrieval")
    graph.add_edge("output", "confidence")

    # Confidence gate: confident answers are returned, weak ones escalated.
    graph.add_conditional_edges(
        "confidence",
        route_after_confidence,
        {
            "final": "final_answer",
            "escalate": "escalate",
        },
    )

    graph.add_edge("final_answer", END)
    graph.add_edge("escalate", END)

    return graph


def compile_app():
    """Compile the graph, adding an in-memory checkpointer when available.

    The checkpointer (MemorySaver) stores per-thread state so follow-up
    questions on the same thread_id see the previous run's context. When
    the checkpointer is unavailable the graph is compiled stateless
    instead — every run is then independent, a graceful degradation. Note
    the state is per-process: the API and the Celery worker each compile
    their own instance with their own MemorySaver.
    """
    graph = build_graph()

    try:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
    except Exception:
        checkpointer = None

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)

    return graph.compile()


# Compiled, ready-to-invoke application used by the FastAPI layer.
workflow_app = compile_app()
