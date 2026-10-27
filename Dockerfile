# ISO Insight — API (FastAPI + LangGraph + MCP)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Zależności i metadane pakietu z pyproject.toml (backend hatchling).
COPY pyproject.toml ./

# Kod aplikacji w układzie src/ (.env celowo NIE trafia do obrazu — patrz
# .dockerignore; konfiguracja jest podawana przez environment / env_file
# w docker-compose).
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Jeden worker: checkpointer MemorySaver trzyma stan wątków w pamięci procesu —
# więcej workerów oznacza rozdrobnione wątki i zduplikowane połączenia MCP.
# Ten sam obraz służy też Celery workerowi (patrz docker-compose.yml).
CMD ["uvicorn", "iso_insight.api.main:api", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
