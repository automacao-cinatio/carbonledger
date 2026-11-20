# Progresso

- [x] P00 Contexto
- [x] P01 Esqueleto
- [ ] P02 Banco e RLS
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
