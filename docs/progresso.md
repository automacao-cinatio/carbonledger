# Progresso

- [x] P00 Contexto
- [x] P01 Esqueleto
- [x] P02 Banco e RLS
- [ ] P03 Contexto de tenant
- [ ] P04 Identidade
- [ ] P05 Trilha e chaves
- [ ] P05A Base visual
- [ ] P06 Estrutura
- [ ] P07 Fatores e GWP
- [ ] P08 Coleta e evidências
- [ ] P09 Escopo 1
- [ ] P10 Escopo 2 e paridade
- [ ] P11 Fechamento
- [ ] P12 Verificador
- [ ] P13 Ferramenta oficial
- [ ] P14 Portal 1a
- [ ] P15 NF-e e CT-e
- [ ] P16 IA
- [ ] P17 Escopo 3
- [ ] P18 Metas
- [ ] P19 PDF assinado
- [ ] P20 Integridade
- [ ] P21 Campanhas
- [ ] P22 Cobrança
- [ ] P23 LGPD
- [ ] P24 Painel
- [ ] P25 Infra AWS
- [ ] P26 CI/CD
- [ ] P27 Backup e go-live

## P01 Esqueleto (concluído em 2026-11-20)

- Pilha de desenvolvimento sobe com docker compose up -d --build --wait: api, worker, beat, postgres, redis, minio (com bucket de evidências em Object Lock), clamav e mailpit, todos saudáveis.
- /saude responde 200 com banco, Redis e MinIO ok; worker responde ao ping do Celery; ClamAV responde PONG.
- Object Lock conferido: o bucket evidencias aceita retenção e uma versão em COMPLIANCE recusa exclusão.
- Postgres sem porta publicada; as portas expostas ficam em 127.0.0.1.
- make check verde: ruff, mypy, pytest (11 testes, com Postgres real via testcontainers), bandit, pip-audit, gitleaks e verificação de caracteres.
- Pendência de ambiente: o make não está instalado no Windows nem no Ubuntu do WSL; os alvos foram executados um a um.

## P02 Banco e RLS (concluído em 2026-11-20)

- Papéis app_owner (dono, só migrações), app_user (API e workers, sem BYPASSRLS) e app_verificador (sem login, só SELECT, via SET LOCAL ROLE), criados por infra/sql/papeis.sql na primeira subida do Postgres.
- Alembic com asyncpg, rodando só como app_owner (o env.py recusa outro papel); make migrate usa o serviço migrar.
- Migração 0001: funções de contexto (app.tenant_id, app.escopo, app.empresas, app.usuario_id), tabelas tenant e empresa, RLS com ENABLE e FORCE, política permissiva de tenant e restritiva de empresa, sem DELETE para o app_user.
- app/core/rls.py gera o SQL de RLS e de permissões; app/core/tenancy.py aplica o contexto com set_config local e recusa fora de transação.
- tests/isolation: cobertura de RLS pelo catálogo (registro em tests/isolation/tabelas.py), suíte de vazamento e migração de ida e volta, com Postgres real. Teste de mutação manual confirmou que a suíte falha sem FORCE ou com a política de empresa aberta.
- API e workers não recebem mais o .env inteiro: só as variáveis listadas no docker-compose.yml, sem a senha do superusuário nem a do app_owner.
- make check verde: 67 testes.
- Próximo: P03 (contexto de tenant na requisição e nas tarefas Celery).
