"""LangGraph workflow wiring (the only place langgraph is imported).

The graph is stateless with respect to construction: :func:`build_workflow`
takes a :class:`PipelineContext` and returns a freshly compiled graph, so
each worker process compiles its own and the API process never needs to.
No module-level singleton — the checkpointer is per-process by design
(MemorySaver), which is why the API runs with a single uvicorn worker.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from iso_insight.agents.nodes import NodeFunctions
from iso_insight.pipeline.context import PipelineContext
from iso_insight.schemas.workflow import WorkflowState

# Generous bound for the critic loop: each iteration runs
# improve -> retrieval -> ... -> reflector, and the node-level
# ``max_iterations`` check is the real budget.
RECURSION_LIMIT = 50


def build_workflow(ctx: PipelineContext) -> Any:
    """Compile the agent workflow over :class:`WorkflowState`."""
    nodes = NodeFunctions(ctx.settings, ctx.llm, ctx.tools, ctx.tracing)

    graph = StateGraph(WorkflowState)
    graph.add_node("orchestrator", nodes.orchestrator)
    graph.add_node("iso_parser", nodes.iso_parser)
    graph.add_node("retrieval", nodes.retrieval)
    graph.add_node("evidence_validator", nodes.evidence_validator)
    graph.add_node("gap_analysis", nodes.gap_analysis)
    graph.add_node("impact_risk", nodes.impact_risk)
    graph.add_node("draft_answer", nodes.draft_answer)
    graph.add_node("reflector", nodes.reflector)
    graph.add_node("improve", nodes.improve)
    graph.add_node("output_validator", nodes.output_validator)
    graph.add_node("confidence_check", nodes.confidence_check)
    graph.add_node("final_answer", nodes.final_answer)
    graph.add_node("escalate", nodes.escalate)

    graph.add_edge(START, "orchestrator")
    graph.add_edge("orchestrator", "iso_parser")
    graph.add_edge("iso_parser", "retrieval")
    graph.add_edge("retrieval", "evidence_validator")
    graph.add_edge("evidence_validator", "gap_analysis")
    graph.add_edge("gap_analysis", "impact_risk")
    graph.add_edge("impact_risk", "draft_answer")
    graph.add_edge("draft_answer", "reflector")
    graph.add_conditional_edges(
        "reflector",
        nodes.route_after_reflector,
        {"output": "output_validator", "improve": "improve"},
    )
    graph.add_edge("improve", "retrieval")
    graph.add_edge("output_validator", "confidence_check")
    graph.add_conditional_edges(
        "confidence_check",
        nodes.route_after_confidence,
        {"final": "final_answer", "escalate": "escalate"},
    )
    graph.add_edge("final_answer", END)
    graph.add_edge("escalate", END)

    try:
        from langgraph.checkpoint.memory import MemorySaver
    except ImportError:  # older langgraph: no checkpointer, no thread persistence
        return graph.compile()
    return graph.compile(checkpointer=MemorySaver())
