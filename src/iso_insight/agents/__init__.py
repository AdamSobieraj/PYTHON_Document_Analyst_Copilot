"""Agent node logic — functions over the workflow state.

Agents talk to external systems only through the ports (LLM, tools,
tracing); they never import infrastructure and never construct
clients.
"""
