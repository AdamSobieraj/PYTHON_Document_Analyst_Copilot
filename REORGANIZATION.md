# Reorganization: flat layout → hexagonal (per CLAUDE.md)

One phase = one session = one commit. The legacy flat modules
(`config.py`, `state.py`, `guardrails.py`, `nodes.py`, `graph.py`, `llm.py`,
`tracing.py`, `mcp_client.py`, `tools.py`, `celery_app.py`,
`iso_multiagent_api.py`) stay working at the repo root until Phase 4
cutover; the new tooling (ruff / mypy / pytest / import-linter) is scoped
to `src/` + `tests/`, so `make check` is green from Phase 1 onward.

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Tooling (`pyproject.toml`, `Makefile`), target skeleton under `src/iso_insight/`, `config.py` (pydantic-settings), `schemas/`, `ports/`, `observability/masking.py`, `exceptions.py`, unit tests | done |
| 2 | `adapters/llm/` (httpx OpenAI-compatible client + JSON extraction), `adapters/tracing/` (langfuse + no-op), versioned prompts `llm/prompts/*_v1.md` + loader, port contract tests with a fake LLM | todo |
| 3 | `adapters/tools/` (MCP manager), `retrieval/` search wrappers, `agents/` nodes via ports, `pipeline/` state helpers + guardrails + LangGraph wiring | todo |
| 4 | `api/`, `workers/`, `bootstrap.py` wiring, integration tests, Dockerfile / compose / README updates, delete the legacy flat modules | todo |

Notes:

- Env var names are unchanged — the existing `.env` keeps working; the new
  `MAX_ITERATIONS` / `LOW_CONFIDENCE_THRESHOLD` vars are documented in
  `.env.example` and have safe defaults in code.
- `make check` = ruff check + ruff format --check + mypy --strict +
  lint-imports + pytest — the single quality gate.
- langgraph is allowed only in `pipeline/` (import-linter contract);
  `ports/` may import only `schemas/` + stdlib + pydantic.
