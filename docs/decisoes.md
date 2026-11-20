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

## 2026-11-20: banco e RLS (P02)

- Contexto em configurações locais da transação, lidas por funções contexto_*(); valor ausente ou vazio vira NULL e nada aparece (falha fechada). Escopos: tenant (todas as empresas) ou empresas (só as listadas; lista vazia não vê nenhuma). Escopo desconhecido não abre nada.
- Políticas chamam as funções dentro de (SELECT ...), para o Postgres avaliar uma vez por consulta e não por linha.
- Uma única política permissiva por tabela (a de tenant): outra permissiva somaria acesso por OR. Restrições adicionais entram como restritivas; o teste de cobertura garante isso.
- A tabela tenant usa tenant_id como chave e a empresa usa chave (tenant_id, empresa_id): o RLS fica uniforme e chaves estrangeiras compostas impedem apontar para empresa de outro tenant.
- tenant e empresa sem DELETE para o app_user (nada some).
- app_user entra no app_verificador com INHERIT FALSE e SET TRUE: assume o papel só com SET LOCAL ROLE e não herda nada dele. O limite por concessão entra no P12 como política restritiva.
- Funções de app/core/rls.py usadas por migração nunca mudam de comportamento; comportamento novo vira função nova.
- Migrações recusam rodar com papel diferente de app_owner, para nenhuma tabela nascer com dono errado.
- API e workers recebem variáveis explícitas no docker-compose.yml em vez do .env inteiro; o serviço migrar é o único com a credencial do app_owner.
- Teste de isolamento com dados de dois tenants e duas empresas cada; o teste de cobertura exige que toda tabela registrada tenha linhas dos dois tenants, para a suíte não passar no vazio.

## 2026-11-20: contexto de tenant (P03)

- Na API, o contexto mora em session.info e é aplicado pelo evento after_begin da SessaoTenant, não uma vez no início da requisição: um commit no meio da rota abre transação nova, e ela também precisa do contexto.
- A sessão de tenant usa Depends com scope="function" (FastAPI 0.165): o commit acontece antes da resposta, então um erro no commit vira 500 em vez de um 200 de gravação perdida.
- 404 também para HTTPException 403 levantada por qualquer código, com o mesmo corpo de rota inexistente, para não distinguir "não existe" de "não pode".
- Tarefas Celery abrem uma conexão asyncpg por execução, sem pool: cada tarefa roda em asyncio.run() próprio, e um pool asyncpg fica preso ao event loop que o criou. Rever se o volume de tarefas pedir.
- A checagem de tenant_id na publicação fica numa subclasse do Celery (send_task), não no sinal before_task_publish: o Celery engole exceções de sinais e a publicação seguiria.
- Tarefa de tenant aceita só argumentos nomeados; tenant_id, empresas e usuario_id são reservados. Sem empresas, o escopo é o tenant; lista vazia de empresas não vê nenhuma (nunca vira o tenant inteiro).
- O log da requisição lê o tenant de request.state: o BaseHTTPMiddleware roda o endpoint em outra task, e os contextvars ligados lá não voltam.

## 2026-11-20: identidade (P04)

- Login só por OIDC com Google e Microsoft, entrada só por convite e quatro perfis (administrador, gestor, responsável, colaborador): decisões do usuário.
- O login lê acessos antes de existir contexto de tenant. Em vez de abrir exceções nas políticas de RLS, criamos o papel app_identidade (sem login, com BYPASSRLS), dono só de cinco funções SECURITY DEFINER com search_path fixo. O app_user tem só EXECUTE nelas; o teste de cobertura trava o papel (sem login, sem tabelas, sem membros além do app_owner, lista fechada de funções).
- usuario e identidade são globais: a pessoa existe antes de escolher um cliente e pode estar em vários. usuario tem RLS próprio (a pessoa vê a si mesma; com contexto de tenant, vê os membros dele); identidade não é legível pelo app_user.
- Na migração, REVOKE e GRANT das funções vêm antes do ALTER OWNER: depois da troca de dono o app_owner não revoga mais nada, e o Postgres só avisa. O teste pegou o EXECUTE para PUBLIC que ficava valendo.
- Convite: o token leva tenant_id, convite_id e um segredo de 32 bytes; o banco guarda só o SHA-256 do segredo e a busca é pela chave (tenant_id, convite_id), sem índice global. Um único erro (CL001) para todo motivo de recusa.
- E-mail só conta quando o provedor garante a posse: email_verified no Google, xms_edov no Microsoft (claim opcional que precisa ser ligada no Entra). Vincular um segundo provedor à mesma pessoa só acontece por convite aceito com o mesmo e-mail.
- Microsoft pelo endpoint "organizations": sem contas pessoais. O emissor é validado contra o tid do próprio token.
- Sessão no Redis em vez de JWT: dá para encerrar e trocar de cliente no servidor. O acesso (perfil e empresas) é relido no banco a cada requisição, em vez de guardado na sessão, para a revogação valer na hora.
- CSRF por cabeçalho X-CSRF-Token com o token da sessão em toda escrita com cookie; falha responde 404, como acesso negado.
- Sem cookie de sessão, o middleware não consulta o Redis: o /saude e as rotas públicas não dependem do armazém de sessões.
- Mensagens de erro de validação com 422 e texto que diz como corrigir; falhas de login respondem 404 sem motivo, e o motivo vai para o log (evento login_falhou, sem dado pessoal).
- Provedor simulado de dev: mock-oauth2-server (navikt), com trava que o proíbe em produção.
- Os testes não leem o .env local (tests/conftest.py desliga o env_file da Configuracao): o resultado não pode depender da máquina de quem roda.
