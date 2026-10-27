UV ?= uv

.PHONY: check lint format types contracts tests test-integration up down

# Quality gate (CLAUDE.md): lint + types + import contracts + tests.
# Unit and contract tests run here; compose-based integration tests
# live behind `test-integration` and `live`-marked tests never run in CI.
check: lint types contracts tests

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

format:
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

types:
	$(UV) run mypy

contracts:
	$(UV) run lint-imports

tests:
	$(UV) run pytest

test-integration:
	$(UV) run pytest -m integration

up:
	docker compose up --build -d

down:
	docker compose down
