# Decisões de arquitetura

## 2026-11-20: decisões assumidas

- Portal em Jinja2 + HTMX servido pelo FastAPI, com o Tabler como base visual: menos JavaScript e nenhum build de frontend.
- uv para dependências; testes com Postgres real via testcontainers.
- MinIO com Object Lock simula o S3 em desenvolvimento; ClamAV e Mailpit em contêiner.
- Modelo da Anthropic definido por variável de ambiente, nunca fixo no código.
- Isolamento no banco: RLS por tenant e por empresa, com falha fechada.
- Cálculo com decimal e pint; GWP AR5 de 100 anos por padrão; fatores bitemporais, nunca UPDATE.
- audit_log só inserção, com hash encadeado por tenant.
- Fora do escopo do MVP: créditos de carbono, SBCE, captura na SEFAZ, SAML e banco dedicado.
## 2026-11-20: ajustes ao subir a pilha local (P01)

- Imagens do MinIO: as oficiais (minio/minio e minio/mc) não estão mais disponíveis no Docker Hub nem no quay.io. Passamos a usar cgr.dev/chainguard/minio e cgr.dev/chainguard/minio-client (variante -dev, que traz shell, mc e wget), compiladas pela Chainguard a partir do código-fonte do MinIO. O serviço continua sendo o MinIO com Object Lock; só muda a origem da imagem. Vale apenas para desenvolvimento; em produção o armazenamento é o S3.
- Healthcheck do MinIO por wget em /minio/health/live, sem depender de alias do mc.
- Celery com o extra redis (celery[redis]): o transporte Redis do kombu importa packaging, que não vinha sem o extra, e o worker caía na partida.
- uvicorn com --reload-dir app: sem isso o recarregador vigiava o /app inteiro, inclusive o .venv, e no bind mount do Windows consumia quase um núcleo parado.
- gitleaks em modo git (histórico de commits) no make check: o modo dir varria o .venv pelo bind mount e passava de 10 minutos.
