# ISO Insight — API (FastAPI + LangGraph + MCP)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Najpierw zależności — dzięki temu cache warstwy przetrwa zmiany w kodzie
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kod aplikacji (.env celowo NIE trafia do obrazu — patrz .dockerignore;
# konfiguracja jest podawana przez environment / env_file w docker-compose)
COPY config.py state.py guardrails.py mcp_client.py tools.py llm.py nodes.py graph.py tracing.py celery_app.py iso_multiagent_api.py ./

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Jeden worker: checkpointer MemorySaver trzyma stan wątków w pamięci procesu —
# więcej workerów oznacza rozdrobnione wątki i zduplikowane połączenia MCP.
CMD ["uvicorn", "iso_multiagent_api:api", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
