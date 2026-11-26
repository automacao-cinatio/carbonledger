.PHONY: up down logs migrate seed test check lint tipos seguranca caracteres backup ensaio-restauracao ensaio-producao ensaio-producao-parar infra

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

# Demonstracao completa com referencia FICTICIA (so na pilha local, com make up antes):
# docs/runbooks/demonstracao.md.
seed: .env
	uv run python scripts/demonstracao.py

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

tipos:
	uv run mypy app scripts/backup.py scripts/ensaio_producao.py scripts/demonstracao.py

seguranca:
	uv run bandit -q -r app scripts/backup.py scripts/ensaio_producao.py scripts/demonstracao.py
	uv run pip-audit
	$(GITLEAKS) git /repo --no-banner

caracteres:
	uv run python scripts/verificar_caracteres.py

# Terraform da AWS (P25): formato, validacao e checagem de seguranca, tudo em container e sem
# conta da AWS (nada e aplicado). Excecoes do Trivy ficam no proprio recurso, com o motivo.
TERRAFORM ?= docker run --rm -v "$(CURDIR)/infra/terraform:/tf" -w /tf hashicorp/terraform:1.13
TRIVY ?= docker run --rm -v "$(CURDIR)/infra/terraform:/tf:ro" aquasec/trivy:0.76.2

infra:
	$(TERRAFORM) fmt -check -recursive
	$(TERRAFORM) init -backend=false -input=false
	$(TERRAFORM) validate
	$(TRIVY) config --quiet --skip-dirs /tf/.terraform --exit-code 1 /tf

check: lint tipos test seguranca caracteres
