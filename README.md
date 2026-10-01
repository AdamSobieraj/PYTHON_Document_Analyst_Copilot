# ISO Insight

**ISO Insight** is a multi-agent, read-only analysis assistant for business analysts working with banking standards such as **ISO 20022**.

It helps analysts faster understand:

- standard requirements,
- changes between versions,
- business process impact,
- compliance gaps,
- risks,
- and which documentation or tickets are relevant.

The system does **not** create or modify Jira issues or Confluence pages. It only reads, analyzes, validates, and returns an answer with sources.

---

## Key Features

- Multi-agent workflow built with **LangGraph**
- Queue-based queries (Celery + Redis): submit a question, poll for the
  result — long analyses never block or drop an HTTP connection
- Per-ISO RAG routing
- MCP-based integrations with:
  - RAG services
  - Jira
  - Confluence
- LLM-powered analysis using a private / on-prem LLM endpoint
- Guardrails for PII and prompt injection
- Evidence validation
- Reflection / critic loop
- Confidence-based escalation
- Audit log
- Read-only by design

---

## Non-Goals

On this version, the system does **not**:

- create Jira issues,
- update Jira issues,
- create Confluence pages,
- edit Confluence content,
- perform automated business actions.

Those can be added later as a controlled, human-approved action layer.

---

## Architecture

```text
+----------------------+
|  User / UI / cURL    |
+----------+-----------+
           |  POST /agent/query
           v
+--------------------------+     blocked (PII / injection)
| API (FastAPI)            |------------------------> 400 to caller
|   Input Guardrails       |
+--------------------------+
           |  allowed -> 202 + task_id back to the caller
           |  publish task
           v
+--------------------------+
| Redis                    |   queue + task results
+--------------------------+
           |  worker consumes
           v
+--------------------------+
| Celery worker            |
+--------------------------+
           |
           v
+--------------------------------------------------------------+
|              LangGraph Workflow (in the worker)              |
|                                                              |
|  Orchestrator                                                |
|         |                                                    |
|         v                                                    |
|  ISO Parser ------------------------------------------------> LLM
|         |                                                    |
|         v                                                    |
|  Retrieval Agent                                             |
|      / |  \                                                  |
|     /  |   \                                                 |
|    v   v    v                                                |
| RAG   Jira  Confluence                                       |
| ISO   MCP   MCP                                              |
| RAG                                                |
| Default / ISO20022 / ISO8583                   |
|         |                                                    |
|         v                                                    |
|  Evidence Validator                                         |
|         |                                                    |
|         v                                                    |
|  Gap Analysis ---------------------------------------------> LLM
|         |                                                    |
|         v                                                    |
|  Impact / Risk --------------------------------------------> LLM
|         |                                                    |
|         v                                                    |
|  Draft Answer ---------------------------------------------> LLM
|         |                                                    |
|         v                                                    |
|  Reflector / Critic ---------------------------------------> LLM
|      / \                                                     |
| pass   fail                                                  |
|   |      |                                                   |
|   v      v                                                   |
| Output Improve --------------------------------------------> LLM
|   |      |                                                   |
|   v      +-------------------> Retrieval                    |
| Confidence                                                  |
|    / \                                                       |
| high  low                                                    |
|   |    |                                                     |
|   v    v                                                     |
| Final Escalation                                             |
|   |    |                                                     |
|   v    v                                                     |
|  END   END                                                   |
+--------------------------------------------------------------+
```

---

## Agent Pipeline

| Agent | Responsibility |
|---|---|
| Orchestrator | Initializes workflow state and routing context |
| ISO Parser | Detects ISO standard, message type, and requirements |
| Retrieval | Calls the correct RAG / Jira / Confluence MCP tools |
| Evidence Validator | Checks whether enough sources were retrieved |
| Gap Analysis | Compares ISO requirements with available business context |
| Impact / Risk | Estimates business impact and risks |
| Draft Answer | Generates a draft answer using LLM |
| Reflector / Critic | Validates the draft answer and requests improvements if needed |
| Improve | Refines the retrieval query when the answer is weak |
| Output Validator | Finalizes the answer and attaches sources |
| Confidence Check | Decides whether to return the answer or escalate |

---

## Per-ISO RAG Routing

Each ISO standard can have its own RAG knowledge base.

Example:

```text
ISO 20022 -> rag_iso20022
ISO 8583  -> rag_iso8583
unknown   -> rag_default
```

The **ISO Parser** detects the standard from the user query.  
The **Retrieval Agent** then selects the matching RAG MCP service.

This reduces the risk of mixing requirements from different ISO standards.

---

## MCP Integration

RAG, Jira, and Confluence are exposed as separate MCP services.

The application calls MCP tools such as:

```text
rag_search
jira_search
confluence_search
```

Supported transports:

```text
streamable_http
sse
```

Example service configuration:

```env
RAG_DEFAULT_MCP_URL=http://rag-default:8000/mcp
RAG_ISO20022_MCP_URL=http://rag-iso20022:8000/mcp
JIRA_MCP_URL=http://jira-mcp-service:8000/mcp
CONFLUENCE_MCP_URL=http://confluence-mcp-service:8000/mcp
```

---

## LLM Integration

The system calls an OpenAI-compatible chat completion endpoint.

Expected endpoint format:

```text
{LLM_BASE_URL}/chat/completions
```

Example:

```env
LLM_BASE_URL=http://llm-service:8000/v1
LLM_MODEL=bank-llm
```

The system expects structured JSON responses where possible and includes fallback parsing for common LLM output issues.

---

## Project Structure

```text
.
├── .env.example
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── config.py
├── state.py
├── guardrails.py
├── mcp_client.py
├── tools.py
├── llm.py
├── nodes.py
├── graph.py
├── tracing.py
├── celery_app.py
└── iso_multiagent_api.py
```

### File Responsibilities

| File | Purpose |
|---|---|
| `iso_multiagent_api.py` | FastAPI application and HTTP endpoints |
| `graph.py` | LangGraph workflow definition |
| `nodes.py` | Agent / node logic |
| `tools.py` | MCP tool wrappers |
| `mcp_client.py` | MCP session management |
| `llm.py` | LLM client and JSON parsing |
| `state.py` | Workflow state schema |
| `guardrails.py` | Input validation |
| `config.py` | Configuration and MCP service registry |
| `tracing.py` | Optional Langfuse tracing (decorator + trace attributes, no-op when off) |
| `celery_app.py` | Celery app + the query task (sole execution path); worker event loop / MCP lifecycle |

---

## Requirements

```text
Python 3.10+
FastAPI
LangGraph
MCP client
httpx
python-dotenv
langfuse   (optional — enables tracing when LANGFUSE_* keys are set)
celery     (query queue)
redis      (Celery broker + result backend)
```

---

## Installation

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Copy the example environment file:

```bash
cp .env.example .env
```

On Windows:

```powershell
copy .env.example .env
```

Edit `.env` with your real service URLs and tokens.

---

## Running the Application

The service has three required pieces — the Redis broker, the Celery
worker (which runs the analyses) and the API (which submits and reports
status). See [Query (Celery + Redis)](#query-celery--redis) for the full
local run instructions.

```bash
# broker (Docker)
docker compose up -d redis-broker

# worker (terminal A) — runs the analyses
celery -A celery_app.celery_app worker --loglevel=info --pool=solo

# API (terminal B) — submits tasks and reports status
uvicorn iso_multiagent_api:api --reload
```

The API will be available at:

```text
http://localhost:8000
```

---

## Docker

```bash
docker compose up -d --build
curl http://localhost:8000/health
```

- `Dockerfile` builds the image used by both the API and the Celery worker (config comes from `.env` via `env_file`, never baked into the image),
- `docker-compose.yml` runs the `api` service on port 8000 with a healthcheck and a shared bridge network `iso-insight_iso-net`,
- the **Celery broker + worker** (`redis-broker`, `worker`) are required — the API only submits tasks, the worker runs the analyses; `docker compose up -d --build` starts everything,
- the **Langfuse observability stack** (web, worker, ClickHouse, Postgres, Redis, MinIO) starts with `docker compose up -d` too. It is fully optional — the service runs without it and degrades gracefully if it is down,
- the upstream services (RAG, Jira, Confluence, LLM) are not part of this repo — either run them on the host / in another stack attached to the network, or uncomment their blocks in `docker-compose.yml` and point them to your images (hostnames must match the `*_URL` values in `.env`),
- if the LLM runs on the host machine (e.g. LM Studio), address it as `http://host.docker.internal:1234/v1` in `LLM_BASE_URL`.

To skip the observability stack, start only the required services:

```bash
docker compose up -d --build api worker redis-broker
```

---

## Tracing (Langfuse)

Optional, self-hosted LLM observability. Every analysis run becomes a
trace; each agent node is a span and each LLM call a generation.

- **Enabled by** setting `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
  `LANGFUSE_HOST` in `.env`. With no keys, tracing is a no-op and the app runs
  exactly as before (zero overhead, nothing imported at call time).
- **No-op safe**: if the Langfuse server is unreachable or errors, tracing
  failures are swallowed and never affect the workflow result.
- **Root span**: `agent.run_query` (in the Celery worker), tagged with
  `user_id`, `session_id` (= the `thread_id`) and service tags, so runs
  group per user and per conversation.
- **Node spans**: one per agent node (`agent.orchestrator`, `agent.retrieval`,
  `agent.draft_answer`, `agent.reflector`, …).
- **Generations**: `llm.chat`, with the model name and (when the provider
  returns them) input/output/total token usage.

### Viewing traces

- **UI**: `http://localhost:3000` (login with the `LANGFUSE_INIT_USER_*` values
  set in `docker-compose.yml`).
- **API (v4)**: Langfuse v4 self-hosted runs in *events* mode, so the classic
  `GET /api/public/traces` endpoint is disabled. Read observations with the v2
  endpoint instead:

  ```bash
  curl -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3000/api/public/v2/observations?limit=100"
  ```

### Note on model / token fields

The `model` and token `usage` fields on generations are populated from the LLM
provider's response. If your LLM endpoint is not backed by a real model (e.g. a
stub), those fields will be empty — this is expected, not a defect. Spans,
names, `user_id`, `session_id` and timing are still fully recorded.

---

## API Endpoints

```text
POST /agent/query      — submit an analysis (202 + task_id, or 400 if blocked)
GET  /agent/tasks/{id} — status and result of a submitted analysis
GET  /agent/health     — MCP connectivity (as seen by the worker)
GET  /health           — infrastructure health (broker + worker)
```

### Health Check

```http
GET /health
```

Infrastructure health for the load balancer. Returns 200 only when the
service can accept and process work (broker up and at least one worker
reachable); otherwise 503, with the details still in the payload:

```json
{
  "status": "ok",
  "broker": "up",
  "workers": 1
}
```

```http
GET /agent/health
```

MCP connectivity as seen by the worker(s) running the analyses (503 when
no worker is alive). Example response:

```json
{
  "status": "ok",
  "workers": 1,
  "connected": [
    "rag_default",
    "rag_iso20022",
    "jira",
    "confluence"
  ],
  "startup_errors": []
}
```

---

## Query (Celery + Redis)

The analysis runs in a Celery worker — the API accepts the question and
returns immediately, and the caller polls for the result. Long runs
(minutes of LLM calls) therefore never block, or get cut off, an HTTP
connection.

```text
POST /agent/query           -> 202 {task_id, thread_id, status_url}   (400 if blocked by guardrails)
GET  /agent/tasks/{task_id} -> {status: QUEUED | RUNNING | SUCCESS | FAILURE, result?, error?}
```

### Submit

```http
POST /agent/query
```

Request body:

```json
{
  "query": "What ISO 20022 fields are required for pacs.008.001.08?",
  "user_id": "analyst_demo",
  "permissions": [
    "read_iso",
    "read_rag",
    "read_jira",
    "read_confluence"
  ]
}
```

202 response:

```json
{
  "task_id": "4e6f1a...",
  "thread_id": "3f2c...",
  "status": "QUEUED",
  "status_url": "/agent/tasks/4e6f1a..."
}
```

Blocked queries (PII, prompt injection) are rejected with a `400` at
submit time, **before** anything is enqueued — the raw query text never
reaches the broker, and no worker slot is consumed. The API is the
single entry point to the workflow, so this boundary check is where the
guardrails are enforced (there is no guardrail node inside the graph).

### Poll for the result

```http
GET /agent/tasks/{task_id}
```

Poll until the status is terminal (results expire after 24 h by default;
an unknown or expired task id is reported as `QUEUED` too):

```json
{
  "task_id": "4e6f1a...",
  "status": "SUCCESS",
  "thread_id": "3f2c...",
  "result": {
    "final_answer": "Based on the available sources, ISO 20022 pacs.008.001.08 requires correct payment instruction identification fields and sender/receiver data. The exact field set depends on the standard version and process context.",
    "audit_status": "ok",
    "confidence": 0.85,
    "sources": [
      {
        "source_type": "rag_iso20022",
        "source_id": "iso20022-pacs.008.001.08",
        "title": "ISO 20022 pacs.008.001.08",
        "url": null,
        "section": "PaymentInstructionInformation",
        "quote": "The payment instruction should contain required identification elements according to the ISO 20022 standard.",
        "score": 0.91
      }
    ],
    "audit_log": [
      { "event": "input_allowed" },
      { "event": "iso_parser_completed", "detected_standards": ["ISO20022"] }
    ],
    "thread_id": "3f2c..."
  },
  "error": null
}
```

On `FAILURE`, `result` is null and `error` carries a generic message —
the full traceback stays in the worker logs. When the broker is
unreachable the endpoints return `503`
("Broker niedostępny. Spróbuj ponownie.").

### Example curl request

```bash
# submit
curl -X POST http://localhost:8000/agent/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What ISO 20022 fields are required for pacs.008.001.08?",
    "user_id": "analyst_demo"
  }'

# poll
curl http://localhost:8000/agent/tasks/4e6f1a...
```

### Running locally (Windows)

Prefork is not available on Windows, so the local worker runs with the
`solo` pool (one task at a time):

```powershell
# 1) broker (Docker; published on 127.0.0.1:6380 — 6379 belongs to the Langfuse redis)
docker compose up -d redis-broker

# 2) worker (terminal A)
celery -A celery_app.celery_app worker --loglevel=info --pool=solo

# 3) API (terminal B)
uvicorn iso_multiagent_api:api --port 8000
```

### Running in Docker

```bash
docker compose up -d --build api worker redis-broker
curl http://localhost:8000/health
```

### Notes and limitations

- Thread state (`thread_id`) lives in the worker's in-memory
  checkpointer: it persists across consecutive tasks **within the same
  worker process**, but is lost when the worker restarts, and with
  several workers a thread may land on different ones. A shared (e.g.
  Postgres) checkpointer — see Roadmap — makes it durable and
  multi-worker safe.
- The worker runs tasks one at a time by design (`-c 1` / `--pool=solo`):
  each task is a full multi-LLM analysis run, and the upstream LLM / MCP
  services are the bottleneck, not CPU.
- Task results are stored in Redis and expire after
  `CELERY_TASK_RESULT_EXPIRES` (default 24 h).
- The worker flushes Langfuse tracing on shutdown.

---

## Quality and Safety

The system uses several control layers:

```text
1. Input guardrails
2. PII detection
3. Prompt injection checks
4. Read-only MCP access
5. Evidence validation
6. Reflector / critic loop
7. Confidence check
8. Escalation when confidence is low
9. Audit log
```

The system is intentionally conservative.  
If it does not have enough sources, it should escalate instead of guessing.

---

## Configuration Reference

### RAG Services

```env
RAG_DEFAULT_MCP_TRANSPORT=streamable_http
RAG_DEFAULT_MCP_URL=http://rag-default:8000/mcp
RAG_DEFAULT_MCP_TOOL=rag_search
RAG_DEFAULT_MCP_TOKEN=
RAG_DEFAULT_MCP_TIMEOUT=30
RAG_DEFAULT_MCP_TOP_K=5
```

```env
RAG_ISO20022_MCP_TRANSPORT=streamable_http
RAG_ISO20022_MCP_URL=http://rag-iso20022:8000/mcp
RAG_ISO20022_MCP_TOOL=rag_search
RAG_ISO20022_MCP_TOKEN=
RAG_ISO20022_MCP_TIMEOUT=30
RAG_ISO20022_MCP_TOP_K=5
```

```env
RAG_ISO8583_MCP_TRANSPORT=streamable_http
RAG_ISO8583_MCP_URL=http://rag-iso8583:8000/mcp
RAG_ISO8583_MCP_TOOL=rag_search
RAG_ISO8583_MCP_TOKEN=
RAG_ISO8583_MCP_TIMEOUT=30
RAG_ISO8583_MCP_TOP_K=5
```

### Jira MCP

```env
JIRA_MCP_TRANSPORT=streamable_http
JIRA_MCP_URL=http://jira-mcp-service:8000/mcp
JIRA_MCP_TOOL=jira_search
JIRA_MCP_TOKEN=
JIRA_MCP_TIMEOUT=30
JIRA_MCP_TOP_K=5
```

### Confluence MCP

```env
CONFLUENCE_MCP_TRANSPORT=streamable_http
CONFLUENCE_MCP_URL=http://confluence-mcp-service:8000/mcp
CONFLUENCE_MCP_TOOL=confluence_search
CONFLUENCE_MCP_TOKEN=
CONFLUENCE_MCP_TIMEOUT=30
CONFLUENCE_MCP_TOP_K=5
```

### LLM

```env
LLM_BASE_URL=http://llm-service:8000/v1
LLM_API_KEY=
LLM_MODEL=bank-llm
LLM_TIMEOUT=60
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=
```

### Langfuse (optional tracing)

```env
# Leave the keys empty to disable tracing entirely.
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-iso-insight
LANGFUSE_SECRET_KEY=sk-lf-iso-insight
```

### Celery / Redis broker (query queue)

```env
# 6380 on the host: 6379 is already used by the Langfuse redis.
# In docker-compose the api/worker services override these to
# redis://redis-broker:6379/0.
CELERY_BROKER_URL=redis://localhost:6380/0
CELERY_RESULT_BACKEND=redis://localhost:6380/0
CELERY_TASK_RESULT_EXPIRES=86400
CELERY_TASK_SOFT_TIME_LIMIT=2400
CELERY_TASK_TIME_LIMIT=2700
```

---

## Troubleshooting

### MCP service not connected

Check:

```bash
curl http://localhost:8000/agent/health
```

If a service appears in `startup_errors`, verify:

- service URL,
- transport,
- token,
- TLS certificates,
- proxy settings,
- firewall rules,
- MCP tool name.

---

### SSE / streaming issues

If responses are buffered or timeout, check:

- load balancer buffering,
- proxy timeout,
- `Content-Type: text/event-stream`,
- MCP transport,
- `streamable_http` vs `sse` compatibility.

---

### LLM returns invalid JSON

The system already includes JSON extraction fallbacks. If this happens often:

- lower `LLM_TEMPERATURE`,
- increase `LLM_MAX_TOKENS`,
- improve the LLM model,
- add stricter structured-output support if available.

---

### Query rejected with 400

`POST /agent/query` returns 400 when the input guardrails block the
query — PII (names, e-mails, phone numbers, IBANs…) or prompt-injection
phrases. The message in `detail` says why. This is expected behavior:
rephrase the question without personal data. Nothing was enqueued, and
no worker slot was used.

---

### Queries: 503 or task stuck in QUEUED

Check:

```bash
curl http://localhost:8000/health
```

- `"broker": "down"` — the Redis broker is not running:
  `docker compose up -d redis-broker` (or point `CELERY_BROKER_URL` at your
  own Redis),
- `"workers": 0` — the Celery worker is not running or is connected to a
  different broker; check its log (`celery -A celery_app.celery_app worker
  --loglevel=info --pool=solo`),
- the API and the worker must agree on `CELERY_BROKER_URL` /
  `CELERY_RESULT_BACKEND`.

Tasks stay in `QUEUED` until a worker picks them up, and results expire
after `CELERY_TASK_RESULT_EXPIRES` (24 h by default) — an unknown or expired
task id is reported as `QUEUED` as well.

---

### Low confidence answers

This is expected when:

- the query is too general,
- RAG sources are missing,
- Jira / Confluence context is missing,
- the ISO standard is not detected,
- the answer cannot be grounded.

Improve by:

- adding better RAG content,
- adding more ISO-specific mappings,
- improving retrieval query refinement,
- adding more business context to Jira / Confluence.

---

## Roadmap

Possible next steps:

```text
1. Persistent checkpointer, for example Postgres
2. Langfuse tracing (done — see "Tracing (Langfuse)")
3. Query queue, Celery + Redis (done — see "Query (Celery + Redis)")
4. Golden test set for ISO 20022
5. Contract tests for MCP services
6. Human-in-the-loop approval flow
7. Optional Jira / Confluence write actions
8. Feedback loop from analysts
9. Hybrid retrieval + reranking
10. PII redaction before LLM calls
11. Prompt and model versioning
```

---

## Design Principles

```text
1. Read-only first
2. Source-grounded answers
3. Explicit escalation
4. Per-ISO knowledge routing
5. MCP-based integration
6. Auditable workflow
7. Conservative LLM usage
```

---

## Summary

**ISO Insight** is a production-oriented, multi-agent analysis system for banking ISO standards.

It combines:

```text
LangGraph + FastAPI + Celery + Redis + MCP + RAG + Jira + Confluence + LLM
```

to help business analysts get faster, source-grounded, auditable answers — without automatically modifying business systems.
