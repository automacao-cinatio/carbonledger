# Progresso

- [x] P00 Contexto
- [x] P01 Esqueleto
- [x] P02 Banco e RLS
- [x] P03 Contexto de tenant
- [x] P04 Identidade
- [x] P05 Trilha e chaves
- [x] P05A Base visual
- [x] P06 Estrutura
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

## P03 Contexto de tenant (concluído em 2026-11-20)

- API: sessão SQLAlchemy por requisição (SessaoTenant) que aplica o contexto em toda transação que abre, pelo evento after_begin; commit no meio da rota não perde o contexto. Commit antes da resposta (Depends com scope="function"), rollback em erro.
- O contexto vem de request.state.contexto_tenant, que a autenticação do P04 vai preencher. Até lá, toda rota de tenant responde 404.
- Acesso negado (AcessoNegado) e qualquer 403 respondem 404 com corpo genérico, igual ao de rota inexistente.
- Celery: tarefa_de_tenant exige tenant_id nomeado, abre conexão asyncpg com transação e contexto e fecha no fim. A publicação (send_task, por onde passam delay e apply_async) recusa tarefa sem tenant_id antes de tocar o broker. Tarefa global só com entrada em TAREFAS_GLOBAIS e justificativa; um teste confere o app inteiro.
- Logs: tenant_id no log da requisição e no da tarefa; usuario_id fica fora. Worker com o mesmo formato JSON da API.
- Testes com Postgres real: cada tenant vê só o seu pela API e pela tarefa, escopo de empresas, conexão volta ao pool sem contexto, commit no meio, rollback em erro.
- make check verde: 106 testes.

## P04 Identidade (concluído em 2026-11-20)

- Login só por OIDC (Google, Microsoft com contas corporativas e, fora de produção, provedor simulado), fluxo de código com PKCE, state amarrado ao navegador por cookie e nonce. id_token validado com joserfc: assinatura RS256 pelo JWKS (com nova leitura em rotação de chave), emissor por provedor, audiência, validade, nonce e azp.
- Entrada só por convite do administrador: vale para o e-mail convidado (verificado pelo provedor), uma vez, por 7 dias. Comando python -m app.cli criar-tenant cria o cliente e o convite do primeiro administrador.
- Perfis administrador, gestor, responsável e colaborador. Administrador e gestor veem o tenant inteiro; responsável e colaborador, só as empresas atribuídas.
- Migração 0002: usuario e identidade (globais, com RLS), membro, membro_empresa e convite (por tenant). Funções identidade_* SECURITY DEFINER do papel app_identidade, únicas portas de leitura antes do contexto de tenant.
- Sessão no Redis (cookie HttpOnly e SameSite=Lax, __Host- em HTTPS; Redis guarda só o hash do token), expiração por inatividade e idade máxima, CSRF por cabeçalho em toda escrita. O acesso é relido no banco a cada requisição: desativar um membro corta o acesso na hora.
- API do administrador: convidar (e-mail por SMTP), listar e revogar convites, listar membros, alterar perfil e empresas, desativar e reativar.
- Runbook docs/runbooks/login-e-acessos.md com a configuração do Google e do Entra ID.
- Testes: OIDC com chaves geradas no teste; funções de login e papel app_identidade no banco; roteiro de ponta a ponta com Postgres, Redis e o provedor simulado reais.
- Pendente para os próximos prompts: telas de login, escolha de cliente e gestão de usuários (P05A e P14); trilha de auditoria de login e convites (P05); limite de tentativas de login.
- Até o portal existir, o login termina em /sessao (JSON).
- make check verde: 180 testes. Teste manual no ambiente de dev: login pelo provedor simulado, convite com CSRF e e-mail no Mailpit.

## P05 Trilha e chaves (concluído em 2026-11-20)

- Migração 0003: audit_log por tenant, com RLS, só SELECT e INSERT para o app_user. A trigger audit_encadear calcula seq, hash anterior, horário e hash (SHA-256, formato v1) sob lock consultivo por tenant; a trigger audit_imutavel recusa UPDATE, DELETE e TRUNCATE, até do dono.
- app/core/audit.py: registro na mesma transação da ação (SQLAlchemy e asyncpg), trava contra dado pessoal na trilha e verificador independente em Python.
- Eventos ligados: tenant.criado, convite.criado, convite.revogado, convite.aceito (na mesma transação do aceite), membro.alterado, membro.desativado, membro.reativado, sessao.entrou e sessao.saiu.
- API de leitura e verificação para administrador e gestor (/api/auditoria e /api/auditoria/verificacao).
- app/core/keys.py: cifra envelope AES-256-GCM, contexto com tenant e finalidade como dado associado, rotação por id da chave mestra. Provedor local em dev; em produção a configuração exige KMS, que entra no P25. Ainda sem uso na aplicação: os consumidores chegam com o certificado do PDF (P19) e as integrações (P21 e P22).
- Runbook docs/runbooks/trilha-de-auditoria.md.
- Testes: cadeia íntegra e adulterações (conteúdo, remoção, elo trocado, recálculo), tentativa de forjar seq e hash, superusuário barrado pela trigger, adulteração com a trigger desligada detectada, RLS da trilha, 30 inserções concorrentes sem buraco na sequência, roteiro de ponta a ponta conferindo os eventos pela API.

## P05A Base visual (concluído em 2026-11-20)

- Bibliotecas em app/web/static/vendor com versão no nome da pasta: Tabler 1.7.5, Tabler Icons 3.52.0 (webfont), htmx 2.0.11 e ECharts 6.1.0. Baixadas do registro npm com a integridade sha512 conferida; MANIFESTO.json fixa o SHA-256 de cada arquivo (scripts/vendor_manifesto.py). axe-core 4.14.1 só nos testes.
- Tema único em app/web/static/css/tema.css: azul-marinho e índigo, Arial ou Liberation Sans, cores do Tabler pelas variáveis previstas. Contraste AA conferido por teste a partir do próprio arquivo.
- Macros em app/web/templates/componentes: campos (texto, número com unidade ao lado, seleção) com validação na hora, botão e formulário HTMX com indicador e rascunho, cabeçalho, próximo passo, estado do inventário, barra de progresso, seletor de empresa, alerta, confirmação irreversível por digitação, glossário, tabela numérica pt-BR e gráfico ECharts com tabela equivalente.
- Telas: /ui (galeria, só fora de produção), /entrar, / (início com próximo passo) e /clientes. Layout com menu na ordem do trabalho, seletor de empresa e faixa de estado do inventário e de progresso. Erros viram página HTML para o navegador e JSON para a API e o HTMX. O login agora termina em /.
- Segurança: CSP estrita (sem inline, sem eval, sem domínio externo) e demais cabeçalhos em toda resposta; HSTS em produção.
- app.js único: CSRF no HTMX, aviso de resultado em região aria-live, marcação do campo inválido, rascunho no navegador, confirmação por digitação, glossário e gráficos com números em pt-BR e animação desligada para quem pede menos movimento.
- Testes: regras de UI do CLAUDE.md checadas nos arquivos (toda macro na /ui, nada inline, nada externo, cor e fonte só no tema, menu na ordem, vendor íntegro); axe-core no Chromium sem violação WCAG 2.2 A e AA nem erro de console em /ui, /entrar e página de erro; interações exercitadas no navegador. CI instala o Chromium.
- make check verde: 313 testes (vários parametrizados por arquivo e por par de cores).

## P06 Estrutura (concluído em 2026-11-20)

- Hierarquia dentro do tenant: grupo, empresa (CNPJ da matriz) e unidade (estabelecimento ou instalação), conforme decidido com o usuário.
- Migração 0004: grupo (RLS por tenant), empresa completa (grupo obrigatório, CNPJ, nome fantasia, CNAE, UF, município, ativa) e unidade (RLS por tenant e empresa) com participação societária e controles operacional e financeiro. Empresas que já existiam vão para um "Grupo principal" do tenant na própria migração. Nada some: empresa e unidade se desativam.
- Validação: CNPJ com dígito verificador, já no formato alfanumérico da Receita (desde julho de 2026), conferido com os exemplos oficiais; CNAE só no formato (a lista oficial entra quando estiver em data/); UF entre as 27; participação de 0 a 100 em Decimal, até 4 casas.
- Limites organizacionais: a unidade guarda os fatos; percentual_consolidado(abordagem, unidade) já calcula quanto entra em cada abordagem do GHG Protocol, para o inventário usar.
- Telas em Organização (item do menu liberado): árvore de grupos e empresas, ficha da empresa com as unidades, formulários com validação na hora, todos os erros do envio de uma vez e próximo passo em cada tela. Administrador e gestor editam tudo; responsável, só as unidades das suas empresas; colaborador só lê.
- Trilha: grupo, empresa e unidade criados, alterados (só nomes dos campos), desativados e reativados.
- Componentes novos na /ui: caixa de marcação, botão-link, etiqueta e ficha; tabela com colunas de link e de situação.
- Sem sessão, qualquer tela leva ao login (antes, mostrava "Página não encontrada").
- Testes: CNPJ, CNAE, participação e consolidação; roteiro de ponta a ponta com os três perfis; axe-core nas 10 telas com sessão, com login real no navegador.
- make check verde: 385 testes. Uma rodada isolada teve os 11 testes de acessibilidade com sessão falhando na preparação, sob carga; duas rodadas seguintes passaram inteiras. A espera do login no navegador subiu para 60 s. Se voltar a acontecer, investigar antes de afrouxar mais.
