# CLAUDE.md — Code Organization Rules

Prescriptive rules for how the code in this repo is organized. New code and refactors must follow these rules.

## Toolchain & quality gates

- Python 3.12, `uv` for dependency management, `ruff` (lint), `mypy --strict`, `pytest`.
- `import-linter` enforces the layer boundaries below; its contracts live in `pyproject.toml` and must be kept.
- Makefile targets: `make check` = lint + types + tests (the single quality gate), `make up` / `make down` = docker compose.
- A task is done only when `make check` is green. Write tests first, then implementation.
- Do not add a dependency without justifying it in the PR.

## Architecture: ports and adapters (hexagonal)

- Domain logic (`pipeline/`, `agents/`, `retrieval/`) communicates with external systems — database, object storage, LLM, tracing, document parsers, external forms — **only** through interfaces defined in `ports/`.
- Implementations of those interfaces live in `adapters/`, one subpackage per concern (`storage/`, `db/`, `parsing/`, `llm/`, `tracing/`, ...).
- Goal: swapping an implementation (different DB, storage, LLM provider, parser) = new adapter + a config value, with **zero changes in domain logic**. If a swap forces changes in domain logic, the port boundary is wrong — fix the port.
- `ports/` has zero infrastructure dependencies: it may import only `schemas/`, stdlib, and pydantic.
- Domain packages (`pipeline`, `agents`, `retrieval`, `schemas`) must not import `adapters`, `api`, `workers`, or infra libraries (e.g. `boto3`, `sqlalchemy`, `celery`, `fastapi`, `openai`, `langfuse`, `openpyxl`) — enforced by `import-linter`.
- Domain logic never constructs clients itself: no `boto3.client(...)`, `create_engine(...)`, `OpenAI(...)`, etc. outside `adapters/` and the composition root.

## Dependency injection & composition root

- All dependencies are injected via constructor or a shared context object (e.g. `PipelineContext` — a dataclass holding all ports + `settings`).
- The **only** place where concrete implementations are chosen is `bootstrap.py` (composition root): `settings → concrete adapters → context`.
- Which adapter is used is selected by configuration (env var, e.g. `STORE_BACKEND`, `LLM_BACKEND`), not by code.
- Delivery layers are thin: API endpoints and worker tasks only build the context via bootstrap and call pipeline stages. No business logic in `api/` or `workers/`.

## Schemas, exceptions, contracts

- Pydantic v2 for all data contracts (`schemas/`).
- Ports raise domain exceptions (`ArtifactNotFound`, `RepositoryError`, `TransientError`); adapters translate vendor/library exceptions into them. Retry logic keys off `TransientError`.
- Resource/artifact key layout (e.g. `{id}/{id}_markdown.md`) is defined in the domain (a function like `artifact_key`), never in an adapter.

## LLM usage

- Prompts are versioned files (e.g. `llm/prompts/<name>_v1.md`); structured-output schemas live next to them. The LLM client itself is an adapter.
- Structural facts (page numbers, line numbers, positions) **never** come from the LLM — only from deterministic metadata (e.g. a page map produced by the parser).
- In tests, use a fake LLM that returns recorded/golden responses.

## Background jobs

- Worker tasks must be idempotent: check whether the output artifact already exists before doing work.
- Never assume shared memory between workers/processes: shared state (indexes, caches) is persisted (e.g. on object storage) and reloaded per process.
- Transient failures (429, timeouts) use retry with exponential backoff + jitter, keyed off the domain's `TransientError`.

## Configuration

- Configuration only via environment variables (pydantic-settings), documented in `.env.example`. No hardcoded config values.

## Privacy & observability

- Never log sensitive content (document content, PII).
- PII (national ID numbers, company/tax numbers, IBAN, e-mail, phone) is masked before anything is sent to external tracing (Langfuse). Masking is a pure-logic module (`observability/masking.py`); the tracing SDK itself is an adapter.
- Results are versioned: they carry spec hash, prompt versions, and pipeline version. Re-processing creates a **new** version of the result — it never overwrites the old one.

## Testing

- Every port has an in-memory/fake implementation **and** a shared contract-test suite parameterized over all implementations (fake + real), e.g. one suite run against both the in-memory store and the real object storage.
- Layout:
  - `tests/unit/` — pure logic (chunking, validation, matching, mapping).
  - `tests/contracts/` — port contract suites.
  - `tests/integration/` — real infra from compose (DB, broker, object storage); eager/in-memory mode for logic tests, plus one test with a real worker.
  - `tests/fixtures/` — a few small sample documents + expected outputs.
- Contract tests for LLM-backed steps use the fake LLM with golden responses, including tests of structured-output schemas.
- Real LLM calls only in tests marked `-m live`, never in CI.
- `eval/` holds accuracy-evaluation scripts run against manually annotated fixtures; a report must be generatable with one command.

## Repository layout (target)

src layout, one package, one module per responsibility:

```
src/<pkg>/
├── config.py        # pydantic-settings
├── schemas/         # Pydantic v2 data contracts
├── ports/           # interfaces (ABC), zero infrastructure imports
├── adapters/        # implementations: storage/, db/, parsing/, llm/, tracing/, ... (only place for infra imports)
├── pipeline/        # stage logic: context.py, stages.py, pure helpers (e.g. artifact keys)
├── agents/          # agent logic (code; LLM only via ports)
├── retrieval/       # chunking, indexing, search
├── llm/             # versioned prompts + structured-output schemas
├── observability/   # masking etc. (pure logic)
├── api/             # thin delivery layer (routes, deps)
├── workers/         # thin delivery layer (task wrappers)
└── bootstrap.py     # composition root: settings → adapters → context
tests/{unit,contracts,integration,fixtures}/
eval/
```

## Working style

- Work in phases: one phase = one session = one commit/PR. Do not exceed a phase's scope.
- Tests first, then implementation; definition of done is `make check` passing.
