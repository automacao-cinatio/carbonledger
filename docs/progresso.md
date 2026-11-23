# Progresso

- [x] P00 Contexto
- [x] P01 Esqueleto
- [x] P02 Banco e RLS
- [x] P03 Contexto de tenant
- [x] P04 Identidade
- [x] P05 Trilha e chaves
- [x] P05A Base visual
- [x] P06 Estrutura
- [x] P07 Fatores e GWP (estrutura; os dados oficiais ainda faltam em data/)
- [x] P08 Coleta e evidências (sem as planilhas oficiais nem a regra de materialidade)
- [x] P09 Escopo 1 (com dados fictícios; os oficiais ainda faltam em data/)
- [x] P10 Escopo 2 e paridade (com dados fictícios; sem caso oficial de paridade ainda)
- [x] P11 Fechamento
- [x] P12 Verificador
- [ ] P13 Ferramenta oficial (bloqueado: falta a planilha oficial em data/templates)
- [x] P14 Portal 1a (parte sem paridade oficial)
- [ ] P15 NF-e e CT-e (bloqueado: faltam os XSD em data/xsd)
- [x] P16 IA
- [x] P17 Escopo 3 (com dados fictícios)
- [x] P18 Metas
- [ ] P19 PDF assinado
- [x] P20 Integridade
- [x] P21 Campanhas
- [ ] P22 Cobrança
- [ ] P23 LGPD
- [x] P24 Painel
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

## P07 Fatores e GWP (concluído em 2026-11-20, sem dados oficiais)

- Por decisão do usuário, construído sem números reais: data/ continua sem fatores, GWP e misturas. Os testes usam valores fictícios em tests/dados_ficticios, marcados como tal.
- Formato dos insumos definido em data/README.md, com modelos só de cabeçalho em data/modelos/: CSV com ";", decimal com vírgula (e notação 1,5E-04), datas dd/mm/aaaa ou ISO, fonte obrigatória.
- Leitura e validação em app/modules/fatores/arquivos.py: todos os erros de uma vez, com linha e coluna; unidade do fator conferida pelo registro pint (app/modules/calculo/unidades.py, com Decimal); vigências sobrepostas e misturas acima de 100% recusadas; hash do conteúdo canônico por linha.
- Migração 0005: lote_importacao, lote_decisao, gwp_versao, fator_versao e mistura_versao, tabelas globais só de inserção (trigger recusa UPDATE, DELETE e TRUNCATE, até do superusuário).
- Papel novo app_curadoria (só insere a referência); o app_user só lê. Serviço curadoria no compose e comandos python -m app.cli fatores importar, lotes, aprovar e rejeitar.
- Bitemporal: vigência escolhe a validade; a aprovação do lote (decidido_em) escolhe o conhecimento. fatores_da_atividade, gwp_do_gas e mistura_na_data respondem "como era conhecido" num instante, com hash do conteúdo; instante_do_banco dá o instante a gravar no cálculo.
- Runbook docs/runbooks/curadoria-de-fatores.md.
- Pendente: os arquivos oficiais (GWP AR5, fatores do Programa, misturas CNPE/MME) e a curadoria deles. Sem isso, o P09 não calcula.

## P08 Coleta e evidências (concluído em 2026-11-23, sem dados oficiais)

- Por decisão do usuário, construído sem as planilhas oficiais e sem a regra de materialidade (data/templates/coleta/ continua vazia): a planilha é gerada pelo sistema e a materialidade é decidida fonte a fonte, com justificativa.
- Migração 0006: inventario (um por empresa e ano, abordagem de consolidação, GWP AR5_100, estado em coleta), fonte_emissao, lancamento e lancamento_decisao (só inserção, em versões), evidencia e planilha_importacao (só inserção). RLS de tenant e de empresa em todas; chaves compostas com empresa_id.
- Triggers: lancamento_checar (periodicidade, fonte ativa, inventário em coleta) e lancamento_decisao_checar (quem lançou não decide; só a versão mais recente é decidida). somente_insercao da 0005 nas tabelas imutáveis.
- Categorias dos escopos 1 e 2 (só estrutura, sem números); energia elétrica sempre mensal. Unidade de medida pelo registro pint, com conferência de grandeza.
- Evidências: limite de tamanho, tipo pelo conteúdo, recusa de macro (.xlsm e vbaProject.bin), ClamAV (INSTREAM, falha fechada), S3 com Object Lock e checksum SHA-256, download com nova conferência do hash. Nome do arquivo só no banco.
- Planilha de coleta em CSV: modelo por inventário com células neutralizadas contra fórmula; importação tudo ou nada, com linha e coluna de cada erro; aceita UTF-8 e Windows-1252; linha igual ao dado atual não cria versão.
- Portal: menu Coleta liberado; telas de inventários, inventário (progresso, fontes, planilha), fonte (períodos, evidências), formulário de fonte e período (lançar, corrigir, aprovar, devolver, histórico). Faixa de estado com o inventário da empresa em foco e o progresso real. Componentes novos na /ui: campo de arquivo, formulário multipart e coluna numérica com casas exatas.
- Trilha: inventario.criado, fonte.criada/alterada/desativada/reativada, lancamento.enviado/aprovado/devolvido, evidencia.enviada, planilha.importada (sem texto livre).
- Configuração nova: EVIDENCIAS_MODO_RETENCAO (COMPLIANCE obrigatório em produção), EVIDENCIAS_RETENCAO_DIAS (1825, provisório), UPLOAD_LIMITE_MB (20). Runbook docs/runbooks/coleta-e-evidencias.md.
- Testes: banco (imutabilidade até do superusuário, triggers, chaves, RLS de escrita), vazamento e cobertura com as 6 tabelas novas, roteiro de ponta a ponta com dois perfis, MinIO real com Object Lock, clamd falso por TCP, arquivos, CSV seguro e validações (com hypothesis), axe-core nas 7 telas novas.
- Verificação: ruff, mypy, 585 testes, bandit, pip-audit e caracteres verdes. O gitleaks rodou em modo diretório nas pastas alteradas (o modo git do Makefile só vê o que já foi commitado). O make continua ausente; os alvos rodaram um a um.
- Pendente: planilhas e regra de materialidade oficiais; confirmar o prazo de retenção das evidências; fatores, GWP e misturas oficiais (P07) para o P09 calcular.

## P09 Escopo 1 (concluído em 2026-11-23, com dados fictícios)

- Por decisão do usuário: motor completo testado com valores fictícios (tests/dados_ficticios/calculo_*.csv), e método fator_direto 1.0.0 (quantidade × fator por gás × GWP × consolidação, mistura pela data da compra, sem densidade nem poder calorífico).
- Motor puro em app/modules/calculo/fator_direto.py: Decimal com 50 dígitos, unidades pelo registro pint, CO2 biogênico fora do escopo, hash canônico da entrada e do resultado, entrada serializável para refazer a conta.
- Migração 0007: fonte_emissao.atividade; chave única de fator_versao com a categoria; calculo (com a entrada em jsonb e os dois hashes), calculo_linha (memória de cálculo) e calculo_pendencia, só de inserção, com RLS de tenant e de empresa.
- Camada de banco em app/modules/calculo/servico.py (asyncpg): só dado aprovado entra; pendências explícitas (falta_dado, sem_atividade, sem_fator, unidade_incompativel, sem_gwp), nunca zero; referência "como conhecida" pelo relógio do banco; consolidação da unidade gravada em cada linha; trilha calculo.executado. Conferência refaz a conta a partir da entrada guardada e compara os hashes.
- P07 ajustado: a validação dos arquivos de fatores considera a categoria na sobreposição de vigência, e há consulta de fatores por categoria e lista de atividades conhecidas.
- Coleta: a fonte ganhou "Combustível ou atividade", escolhido entre os fatores e misturas aprovados.
- Portal: menu Resultados liberado; totais, CO2 biogênico, método, instante de conhecimento, hashes, pendências com como resolver, gráfico por categoria com tabela, tabela por gás, histórico de cálculos, memória de cálculo em CSV (sem arredondar) e botão Conferir. Tarefa Celery calculo.calcular_inventario com a mesma função.
- Runbook docs/runbooks/calculo-de-emissoes.md (antes calculo-do-escopo-1.md); data/README.md explica como o cálculo procura fatores e misturas.
- Testes: caso feito à mão (mistura 85/15, consolidação 50%, biogênico), conversão de unidade, pendências, hash estável em qualquer ordem e sensível a qualquer mudança, refazer pela entrada guardada, proporcionalidade com hypothesis; no banco, o caso da semente com valor exato, bitemporalidade, consolidação por participação e isolamento entre tenants; roteiro do portal até a memória e a conferência; axe-core em /resultados.
- Verificação: ruff, mypy, 641 testes, bandit, pip-audit, gitleaks (modo diretório nas pastas alteradas) e caracteres verdes.
- Pendente: fatores, GWP e misturas oficiais em data/ e a curadoria deles; escopo 2 (P10); cadeia da ferramenta oficial com densidade e poder calorífico (P13).

## P10 Escopo 2 e paridade (concluído em 2026-11-23, com dados fictícios)

- Escopo 2 pela abordagem de localização: consumo × fator do SIN do mês, com o mesmo método. O fator do SIN entra como fator comum, mês a mês, na categoria escopo2.energia_eletrica (formato em data/README.md). Energia térmica e perdas na transmissão seguem a mesma conta.
- Método fator_direto 1.1.0, com total do escopo 2 separado (migração 0008: calculo.escopo2_tco2e). As execuções 1.0.0 continuam conferíveis com o hash antigo.
- Montagem da entrada separada em app/modules/calculo/montagem.py, com a referência por interface: o banco (ReferenciaDoBanco) e os casos de paridade (ReferenciaDoCaso) passam pelo mesmo código.
- Paridade em app/modules/calculo/paridade.py e tests/parity: formato do caso em data/paridade/<caso>/, comparação por indicador (escopos, biogênico, categoria e gás) com tolerância em configuração (PARIDADE_TOLERANCIA_TCO2E, 0,001 tCO2e). Caso fictício feito à mão prova que o comparador acerta e sabe falhar; o teste dos casos oficiais fica pulado, com o motivo, até haver caso em data/paridade.
- Resultados: total do escopo 2 na ficha, no aviso e no histórico; gráfico e tabelas por categoria e gás cobrem os dois escopos.
- Runbook renomeado para docs/runbooks/calculo-de-emissoes.md, com a seção de paridade.
- Verificação: ruff, mypy, 648 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.
- Pendente: fator mensal do SIN (MCTI) em data/fatores/sin/; 3 a 5 casos calculados na ferramenta oficial em data/paridade/; confirmar a tolerância com esses casos.

## P11 Fechamento (concluído em 2026-11-23)

- Decisões de produto tomadas por mim e registradas em docs/decisoes.md (o usuário seguiu sem responder): fecha só sem pendências; fecham administrador e gestor; reapresentação reabre o mesmo inventário com motivo, e o próximo fechamento é a versão seguinte.
- Migração 0009: estado fechado; inventario_evento (fechamento e reabertura, só inserção) com versão, cálculo, hash do fechamento e motivo; trigger que só deixa o estado mudar com o evento na mesma transação (CL021); triggers que barram qualquer escrita em fonte, decisão, evidência, planilha e cálculo de inventário fechado (CL020).
- app/modules/fechamento/servico.py: condições conferidas com o inventário travado (cálculo feito, cálculo atualizado pelo hash da entrada, sem pendências, conferência), aviso de período sem evidência, hash do fechamento reproduzível, fechar (nome da empresa conferido no servidor) e reabrir com motivo. Trilha inventario.fechado e inventario.reaberto.
- Portal: menu Fechamento liberado; condições com situação e detalhe, confirmação irreversível digitando o nome da empresa (a macro agora envia o texto para o servidor conferir), ficha do fechamento com o hash, formulário de reapresentação e histórico de fechamentos. A faixa de estado mostra "Fechado".
- Runbook docs/runbooks/fechamento-e-reapresentacao.md.
- Testes: condições e fechamento com hash reproduzível, cálculo desatualizado não fecha, inventário fechado congela tudo (até o cálculo), estado não muda sem evento, reapresentação gera versão 2 com trilha; roteiro do portal recusando fechar com pendência; axe-core em /fechamento.
- Verificação: ruff, mypy, 668 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.

## P12 Verificador (concluído em 2026-11-23)

- Decisões tomadas por mim e registradas em docs/decisoes.md: só o administrador concede; um inventário fechado por concessão; prazo padrão de 90 dias (até 365), revogável.
- Migração 0010: concessao_verificacao (RLS de tenant e empresa); contexto app.concessao_id; funções verificacao_inventario e verificacao_empresa; política restritiva TO app_verificador em todas as tabelas de tenant (só o recorte da concessão, ou nada); leitura da referência global pelo verificador; funções identidade_aceitar_concessao e identidade_concessoes do app_identidade, revisadas.
- Contexto: ContextoTenant com concessao_id aplica app.concessao_id e SET LOCAL ROLE app_verificador na mesma transação, na API (after_begin) e no asyncpg; exige uma empresa e o usuário.
- Sessão e middleware: a sessão guarda a concessão aberta; o acesso do verificador é relido no banco a cada requisição (revogou, perdeu).
- Login: link de uso único /verificacao/convite/<token>/<provedor>, com aceite no banco (e-mail, prazo, revogação, hash do segredo) e trilha concessao.aceita; depois, a verificação aparece na escolha de cliente.
- Portal: menu Verificação liberado (concessões da empresa em foco, conceder com e-mail e prazo, revogar com confirmação); painel do verificador só de leitura (fechamento, cálculo do fechamento, memória em CSV, fontes, fontes fora da materialidade, evidências com SHA-256). Para o verificador, toda tela do cliente responde 404.
- Runbook docs/runbooks/verificacao.md.
- Testes: suíte de vazamento com o recorte do verificador em cada tabela, concessão inválida, de outra pessoa, revogada ou vencida não abre nada, outro inventário da mesma empresa invisível; cobertura exige a política do verificador em toda tabela; contexto do verificador; roteiro de ponta a ponta (conceder, aceitar, painel, 404 fora dele, link de uso único, reabrir, revogar); axe-core na tela de concessões, na escolha de cliente, no painel e na fonte do verificador, com login real no navegador.
- Verificação: ruff, mypy, 732 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.

## P13 Ferramenta oficial (pulado por enquanto)

- Bloqueado: precisa da ferramenta oficial do Programa (Ciclo 2027) em data/templates/. Sem ela, não há como importar ou exportar no formato oficial sem inventar células.

## P14 Portal 1a (concluído em 2026-11-23, sem a parte de paridade oficial)

- Usuários e acessos (/usuarios, só administrador, link no menu do usuário): pessoas com perfil, empresas e situação; convidar com perfil e empresas (e-mail com o link); revogar convite; ficha da pessoa com troca de perfil e empresas, desativar e reativar; o próprio acesso não muda.
- Início com a jornada da empresa em foco (organização, coleta, resultados, fechamento, verificação), totais do último cálculo e o próximo passo.
- Limite de tentativas de login por IP no Redis (LOGIN_LIMITE_TENTATIVAS e LOGIN_JANELA_MIN), com 429 e o tempo de espera; vale para entrar, convite e concessão.
- Componente novo campo_opcoes na /ui.
- Testes: roteiro de usuários (convite com validação, colaborador sem acesso à tela, troca de perfil, desativação que corta o acesso, reativação, revogação de convite), jornada do início, limite com IP próprio, contador com Redis falso, axe-core em /usuarios.
- Verificação: ruff, mypy, 740 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.

## P15 a P17 (ainda não feitos)

- P15 NF-e e CT-e: bloqueado, precisa dos XSD oficiais em data/xsd/.
- P16 e P17 feitos depois do P18.

## P18 Metas (concluído em 2026-11-23)

- Migração 0011: meta (escopos 1, 2 ou 1+2; ano-base de inventário fechado com o valor copiado do cálculo do fechamento; ano-alvo; redução percentual informada pelo cliente; arquivável) e acao_reducao (redução estimada pelo cliente, ano de início, situação). RLS de tenant e empresa; o verificador não vê nada.
- Acompanhamento com trajetória linear e realizado de cada ano (fechamento ou último cálculo, marcado preliminar); gráfico ECharts com a tabela equivalente; comparação do plano de ações com a redução da meta.
- Portal: menu Metas liberado; lista e criação (administrador e gestor), ficha da meta, plano de ação com cadastro e situação (também o responsável), arquivar e reativar. Trilha meta.criada, meta.arquivada, acao.criada, acao.alterada.
- Testes: trajetória e situação (unitários), roteiro no portal (validações, valores da meta, cobertura do plano, situação da ação, arquivar), isolamento e cobertura das duas tabelas, axe-core em /metas e na ficha da meta.
- Verificação: ruff, mypy, 773 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.

## P17 Escopo 3 (concluído em 2026-11-23, com dados fictícios)

- As 15 categorias do escopo 3 do GHG Protocol no catálogo da coleta (só estrutura).
- Método fator_direto 1.2.0 com total do escopo 3 separado (migração 0012: calculo.escopo3_tco2e); versões 1.0.0 e 1.1.0 continuam conferíveis.
- Moeda (BRL) no registro do pint, para fatores por valor gasto; t·km para frete.
- Resultados, histórico, aviso e painel do verificador com o escopo 3; paridade com o indicador escopo3_tco2e.
- Testes: total do escopo 3 com fator por valor gasto, compatibilidade da 1.1.0, moeda como grandeza própria, segundo caso fictício de paridade (km, BRL e t·km, com consolidação de 50%) feito à mão, cálculo no banco com uma fonte de viagens.
- Verificação: ruff, mypy, 778 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.

## P16 IA (concluído em 2026-11-23)

- Cliente da Anthropic (app/modules/ia/cliente.py): modelo só por ANTHROPIC_MODELO; saída em JSON Schema; esforço baixo; limite de tempo e novas tentativas; reserva do servidor em caso de recusa (IA_FALLBACKS); recusa, resposta cortada, JSON inválido e erros de API ou de rede viram aviso. Sem chave ou modelo, a IA fica desligada.
- Sugestões (app/modules/ia/sugestoes.py): categoria e atividade de uma fonte nova, restritas às opções que existem; opinião sobre um valor fora do padrão para quem decide o dado. Nada é preenchido, gravado, calculado ou aprovado pela IA.
- Telas: botão "Sugerir categoria com IA" na fonte nova e "Pedir a opinião da IA" no período, só para quem decide e só com IA configurada, com aviso do que é enviado. Trilha ia.consultada sem o conteúdo.
- Testes: cliente com a API simulada (pedido, modelo, esquema, reserva, recusa e erros), sugestões restritas às opções, opinião, roteiro no portal com IA falsa (sem IA não há botão nem rota; IA fora do ar vira aviso; trilha sem texto).
- Verificação: ruff, mypy, 797 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes. Sem chamada real à API: falta a chave no ambiente.

## P20 Integridade (concluído em 2026-11-23)

- app/modules/integridade/servico.py: verificação por tenant da trilha (cadeia v1), dos cálculos (refeitos pela entrada guardada), do hash do fechamento atual de cada inventário fechado e do SHA-256 de cada evidência e planilha relida do S3. Resultado só por inserção e evento integridade.verificada; falha vira log de erro sem dado pessoal.
- Migração 0013: integridade_verificacao (RLS de tenant, verificador sem acesso, "íntegra" amarrada à lista de falhas) e identidade_tenants() (só ids, revisada) para o disparo.
- Worker: integridade.verificar_tenant (de tenant) e integridade.disparar (global, justificada), agendado no beat todo dia às 3h.
- Portal: tela Integridade (administrador e gestor, menu do usuário) com a última verificação, o que foi conferido, as falhas, o histórico e "Verificar agora".
- Runbook docs/runbooks/integridade.md.
- Testes: detecção de cálculo sem entrada e de arquivos com hash diferente ou sumidos, resultado inconsistente recusado, disparo só com ids, disparo enfileirando um por tenant, roteiro do portal (tudo confere; evidência adulterada vira falha na tela), axe-core em /integridade.
- Verificação: ruff, mypy, 818 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.

## P24 Painel (concluído em 2026-11-23)

- app/modules/relatorios/painel.py: para cada empresa visível, o inventário mais recente, a situação, o progresso da coleta e as emissões dos escopos 1, 2 e 3 (do fechamento atual ou, sem ele, do último cálculo, preliminar); série anual somando os inventários com cálculo.
- Tela /painel (menu Painel, depois de Metas) com filtro por grupo, tabela de empresas, gráfico de barras empilhadas por empresa e por ano, cada um com a tabela equivalente; próximo passo aponta a primeira empresa sem inventário fechado.
- Testes: roteiro com empresa fechada, empresa sem inventário, inventário em coleta sem cálculo fora da série, filtro por grupo e grupo desconhecido; axe-core em /painel; ordem do menu atualizada.
- Verificação: ruff, mypy, 822 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.

## P21 Campanhas (concluído em 2026-11-23)

- Migração 0014: campanha (prazo, recado, encerramento com motivo; uma aberta por inventário) e campanha_envio (só inserção, um por tipo), com RLS de tenant e empresa e verificador sem acesso.
- app/modules/coleta/campanhas.py: abrir (aviso à equipe com o que falta), encerrar, rodada diária com lembretes de 7, 3 e 1 dia e vencimento, encerramento automático quando a coleta acaba ou o inventário fecha. Trilha campanha.aberta, campanha.aviso_enviado, campanha.encerrada.
- Worker: campanhas.lembrar_tenant e campanhas.disparar (global, justificada), no beat às 8h.
- Tela do inventário: card da campanha (abrir com prazo e recado; prazo, avisos enviados e encerrar).
- Testes: regra do lembrete do dia, abertura com e-mail ao responsável e lista do que falta, campanha repetida recusada, lembrete uma vez só e vencimento, encerramento automático, prazo no passado recusado, disparo, roteiro no portal.
- Verificação: ruff, mypy, 862 testes (1 pulado: paridade oficial sem caso), bandit, pip-audit, gitleaks (modo diretório) e caracteres verdes.
