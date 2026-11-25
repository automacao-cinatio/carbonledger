.PHONY: up down logs migrate seed test check lint tipos seguranca caracteres backup ensaio-restauracao ensaio-producao ensaio-producao-parar

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

# Ensaio de producao local (imagem de producao, AMBIENTE=producao, TLS, KMS local):
# docs/runbooks/ensaio-de-producao.md.
ensaio-producao: .env
	uv run python scripts/ensaio_producao.py subir

ensaio-producao-parar:
	uv run python scripts/ensaio_producao.py derrubar

seed:
	@echo "Sem dados de carga ainda: entram nos prompts P06 e P07."

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

tipos:
	uv run mypy app scripts/backup.py scripts/ensaio_producao.py

seguranca:
	uv run bandit -q -r app scripts/backup.py scripts/ensaio_producao.py
	uv run pip-audit
	$(GITLEAKS) git /repo --no-banner

caracteres:
	uv run python scripts/verificar_caracteres.py

check: lint tipos test seguranca caracteres
