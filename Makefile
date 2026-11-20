.PHONY: up down logs migrate seed test check lint tipos seguranca caracteres

COMPOSE ?= docker compose
GITLEAKS ?= docker run --rm -v "$(CURDIR):/repo" zricethezav/gitleaks:latest

.env:
	cp .env.example .env

up: .env
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

migrate:
	@if [ -f alembic.ini ]; then $(COMPOSE) run --rm api alembic upgrade head; \
	else echo "Sem migracoes ainda: o Alembic entra no P02."; fi

seed:
	@echo "Sem dados de carga ainda: entram nos prompts P06 e P07."

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

tipos:
	uv run mypy app

seguranca:
	uv run bandit -q -r app
	uv run pip-audit
	$(GITLEAKS) dir /repo --no-banner

caracteres:
	uv run python scripts/verificar_caracteres.py

check: lint tipos test seguranca caracteres
