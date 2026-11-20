# CarbonLedger

Plataforma da CINATIO para inventário de emissões de GEE e plano de descarbonização de empresas. Créditos de carbono entram na Fase 2. A primeira entrega é o inventário no padrão do Programa Brasileiro GHG Protocol, pronto para verificação por terceiro e para o Registro Público de Emissões.

## Princípios inegociáveis
- A plataforma calcula e organiza; nunca certifica. Quem atesta é o verificador.
- Todo número é reproduzível: mesma entrada, mesmos fatores, mesmo hash.
- A IA nunca calcula emissão nem aprova dado; ela só sugere.
- Nada some: dado aprovado é só inserção; inventário fechado muda só por reapresentação.
- Isolamento mora no banco: RLS por tenant e por empresa, com falha fechada.
- Nunca invente fator, GWP, percentual de mistura ou célula da ferramenta oficial: leia de data/ ou pare e pergunte.

## Stack
- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 e Alembic na API; asyncpg direto nos workers.
- PostgreSQL 16 com RLS; Redis; Celery com beat.
- S3 (MinIO com Object Lock em dev) e KMS (KeyProvider local em dev).
- Portal em Jinja2 + HTMX com Tabler (Bootstrap 5) e Tabler Icons servidos pelo próprio app; gráficos com Apache ECharts.
- PDF com Playwright; assinatura PAdES com pyHanko.
- Cálculo com decimal e pint; XML fiscal com lxml, defusedxml e xmlschema.
- Ferramentas: uv, ruff, mypy, pytest, pytest-asyncio, hypothesis, testcontainers, bandit, pip-audit, gitleaks, axe-core via Playwright.

## Estrutura
- app/core: config, db, tenancy, security, keys, audit
- app/modules/<modulo>: organizacao, fatores, coleta, calculo, fechamento, verificacao, relatorios, metas, ia, cobranca, lgpd
- app/api: routers por módulo
- app/workers: app Celery e tarefas por módulo
- app/web: templates Jinja2 (componentes/ com as macros), static/css/tema.css e static/vendor/
- migrations; tests/unit, tests/integration, tests/isolation, tests/parity, tests/e2e
- data: templates, fatores, gwp, misturas, paridade, xsd (insumos versionados)
- infra/terraform, infra/sql
- docs: progresso.md, decisoes.md, usabilidade.md, runbooks/

## Multi-tenancy (obrigatório)
- Toda tabela de negócio tem tenant_id NOT NULL e, quando aplicável, empresa_id; índices começam por tenant_id.
- RLS com ENABLE e FORCE em todas; política de tenant permissiva e política de empresa restritiva.
- Contexto por transação com set_config(nome, valor, true): app.tenant_id, app.escopo, app.empresas, app.usuario_id. Nunca SET de sessão.
- API e workers conectam como app_user (sem BYPASSRLS e sem ser dono). Migrações rodam como app_owner.
- Verificador: SET LOCAL ROLE app_verificador (só SELECT), limitado à concessão.
- Tarefa Celery sem tenant_id explícito é bug.
- Acesso negado responde 404, nunca 403.
- Tabela nova com tenant_id entra no teste de cobertura de RLS e na suíte de vazamento no mesmo PR.

## Cálculo
- Decimal sempre; float proibido em quantidade, fator e resultado.
- Unidades só pelo registro pint de app/modules/calculo/unidades.py.
- Métodos com versão semântica; mudar método cria versão nova.
- Fatores bitemporais: nunca UPDATE, sempre nova versão aprovada.
- GWP por inventário; padrão AR5 de 100 anos, como o Programa usa.
- Mistura de combustível pela data da compra; CO2 biogênico separado dos escopos.
- Paridade com a ferramenta oficial em tests/parity; tolerância em configuração, nunca afrouxada para passar.

## Segurança
- XML só com defusedxml; upload com limite, checagem de tipo pelo conteúdo e ClamAV; rejeitar .xlsm.
- CSV exportado neutraliza fórmula (células que começam com =, +, - ou @).
- Segredo só por variável de ambiente; nunca em código, log, commit ou mensagem.
- audit_log só inserção, com hash encadeado por tenant.
- Logs estruturados com tenant_id e request_id; nada de dado pessoal em log ou Sentry.
- Nome do modelo da Anthropic só por variável de ambiente.
- Content-Security-Policy estrita; nenhum arquivo carregado de domínio externo.

## Regras de produção herdadas
- Celery usa asyncpg direto, não SQLAlchemy (conflito de event loop com prefork).
- JSONB mutado exige flag_modified().
- Cifrão no docker-compose vira $$.
- Worker e beat exigem rebuild da imagem, não restart, depois de mudança de código.
- JavaScript em template: no máximo dois níveis de template literal; no terceiro, concatenação.
- Remover ?ssl=disable da DATABASE_URL antes do asyncpg.connect(); em produção, TLS até o banco.
- Postgres só em rede privada, nunca em 0.0.0.0.

## Estilo
- Português do Brasil em UI, mensagens, docs e relatórios. Identificadores em português sem acento, snake_case.
- Proibidos os caracteres U+2013 e U+2014 em qualquer arquivo; o make check verifica.
- UI em Arial/Liberation Sans, paleta azul-marinho e índigo, conforme a seção UI e UX.

## UI e UX
- Tabler e Tabler Icons com versão fixada em app/web/static/vendor; skill do Tabler instalada; padrões das páginas de exemplo antes de CSS próprio.
- Tema único em app/web/static/css/tema.css; nenhuma cor ou fonte fora dele.
- Telas só com as macros de app/web/templates/componentes; componente novo entra primeiro na página /ui.
- Menu na ordem do trabalho; seletor de empresa sempre visível; cada tela termina com o próximo passo.
- Estado do inventário e progresso de coleta e de evidência sempre visíveis.
- Rótulo acima do campo, unidade ao lado do número, validação na hora com mensagem que diz como corrigir; nada digitado se perde.
- Números em pt-BR (1.234,56), unidade no cabeçalho da coluna, números alinhados à direita.
- Ação irreversível pede confirmação que mostra o efeito; fechar inventário exige digitar o nome da empresa.
- Toda ação HTMX com indicador, aviso de resultado e aria-live; htmx com includeIndicatorStyles desligado por causa da CSP.
- Acessibilidade WCAG 2.2 AA: contraste, foco visível, teclado, cor nunca sozinha; axe-core no CI.
- Gráficos só com Apache ECharts; ApexCharts não entra (licença dupla desde a versão 5).
- Termos técnicos com dica de glossário; linguagem simples.

## Fluxo de trabalho
- Plano curto antes de editar; make check verde antes de encerrar.
- Ao fim de cada prompt: atualizar docs/progresso.md, registrar decisões em docs/decisoes.md e propor a mensagem de commit.
- Não implementar nada de fases futuras (créditos, SBCE, captura na SEFAZ, SAML, banco dedicado) sem pedido explícito.

## Comandos
- make up, make down, make logs, make migrate, make seed, make test, make check
