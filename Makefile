.PHONY: up down logs migrate seed test check lint tipos seguranca caracteres backup ensaio-restauracao

COMPOSE ?= docker compose
# Modo git: varre o historico de commits (rapido e sem o .venv).
GITLEAKS ?= docker run --rm -v "$(CURDIR):/repo" zricethezav/gitleaks:latest

.env:
	cp .env.example .env

up: .env
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

migrate: .env
	$(COMPOSE) run --rm migrar

# Backup cifrado do banco e dos objetos em backups/ e ensaio de restauracao numa copia
# descartavel: docs/runbooks/backup-e-restauracao.md.
backup: .env
	uv run python scripts/backup.py criar

ensaio-restauracao: .env
	uv run python scripts/backup.py ensaio

seed:
	@echo "Sem dados de carga ainda: entram nos prompts P06 e P07."

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

tipos:
	uv run mypy app scripts/backup.py

seguranca:
	uv run bandit -q -r app scripts/backup.py
	uv run pip-audit
	$(GITLEAKS) git /repo --no-banner

caracteres:
	uv run python scripts/verificar_caracteres.py

check: lint tipos test seguranca caracteres
