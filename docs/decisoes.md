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

## 2026-11-20: trilha e chaves (P05)

- O hash é calculado no banco (trigger), não na aplicação: nenhum caminho de escrita, nem a linha de comando nem um worker com defeito, grava evento fora da cadeia. O que a aplicação mandar em seq, hash, evento_id e ocorrido_em é descartado.
- A verificação é feita em Python, refazendo o formato v1 a partir dos campos. Ela não usa a função do banco, para que um defeito ou uma troca da função não passe despercebido.
- Serialização da cadeia por pg_advisory_xact_lock por tenant: tenants diferentes gravam em paralelo; o mesmo tenant grava em fila até o commit.
- audit_log sem empresa_id próprio: com a política restritiva de empresa, um responsável não conseguiria gravar eventos do tenant (entrada e saída). Quando o evento é de uma empresa, o id vai em dados. A leitura é só para administrador e gestor.
- Sem dado pessoal na trilha (trava por nome de campo), porque ela nunca é apagada; os eventos guardam identificadores.
- convite.aceito é gravado na mesma transação do aceite; a entrada e a saída de sessão, numa transação curta com o contexto do próprio usuário.
- Chaves: cifra envelope com chave de dados por valor e contexto (tenant_id e finalidade) como dado associado, para um segredo não abrir em outro cliente. Em produção, a configuração recusa o provedor local; o KMS entra no P25, junto com a infraestrutura AWS. A chave local de dev é de propósito 32 bytes zero.

## 2026-11-20: base visual (P05A)

- Bibliotecas de terceiros copiadas byte a byte do pacote npm (integridade sha512 conferida no download) e fixadas por SHA-256 num manifesto; o .gitattributes marca o vendor como binário para o Git não trocar finais de linha. Nenhum arquivo de terceiro é editado.
- Ícones pela webfont do Tabler Icons, dividida por categoria com unicode-range: o navegador baixa só as fontes usadas.
- O Tabler 1.7 expõe os componentes do Bootstrap em window.tabler, não em window.bootstrap. O teste no navegador pegou o glossário e o fechamento de modal quebrados por isso.
- O cinza secundário do Tabler (gray-500) não alcança 4,5:1 sobre fundos tingidos (axe-core apontou). O tema aponta --tblr-secondary para o texto suave da paleta.
- Barra de progresso com o elemento progress nativo (que o Tabler estiliza), porque a barra do Bootstrap define a largura com estilo inline, que a CSP bloqueia. Pelo mesmo motivo, a altura dos gráficos fica no CSS.
- Gráficos: o Python manda os valores como texto (Decimal nunca vira float); o ECharts lê o número, e as cores vêm das variáveis do tema.css.
- Erros por campo voltam como 422 com {campo, mensagem}; o aviso de sucesso vem no cabeçalho X-Aviso (codificado, porque cabeçalho HTTP não leva acento) e aparece numa região aria-live.
- Rascunho de formulário no localStorage do navegador, apagado quando o envio dá certo: nada digitado se perde numa queda de sessão.
- O teste de acessibilidade injeta o axe-core pelo protocolo do DevTools, porque a CSP do app bloqueia script injetado, o que confirma que ela está valendo.
- Seleção de empresa no topo guardada na sessão: é foco de interface; o que cada pessoa pode ver continua definido pelo perfil, no banco.
- Exceção à regra dos caracteres U+2013 e U+2014: as duas pastas vendor (app/web/static/vendor e tests/e2e/vendor) ficam fora da verificação, porque ECharts, Tabler e axe-core os contêm e esses arquivos precisam continuar idênticos aos pacotes oficiais, travados pelo manifesto de hashes. Todo arquivo nosso continua verificado. Para reverter, remova PASTAS_DE_TERCEIROS em scripts/verificar_caracteres.py.

## 2026-11-20: estrutura (P06)

- Grupo, empresa e unidade (decisão do usuário). O grupo só tem RLS de tenant; o responsável vê só os grupos que têm empresa dele.
- Limites organizacionais: a unidade guarda participação societária e os controles; a abordagem é escolhida em cada inventário (decisão do usuário). A regra fica numa função pura, testada, em app/modules/organizacao/limites.py.
- CNAE em texto validado só no formato até a lista oficial entrar em data/ (decisão do usuário).
- CNPJ alfanumérico: o banco confere o formato (12 posições 0-9 ou A-Z e 2 dígitos); o dígito verificador é conferido no app. CNPJ fica opcional no banco (registros antigos) e obrigatório no formulário da empresa.
- Migração com dados: para mover empresas existentes para o grupo padrão, a migração tira o FORCE do RLS da empresa (o dono da tabela passa a enxergar tudo) e o devolve na mesma transação.
- Formulários devolvem todos os erros de uma vez (ColetorDeErros e ErrosDeValidacao), e o aviso diz quantos campos corrigir.
- A trilha guarda só os nomes dos campos alterados, nunca os valores: razão social pode ser nome de pessoa (MEI).
- Sem sessão, telas levam ao login: com a sessão expirada, a pessoa entende o que aconteceu.

## 2026-11-20: fatores e GWP (P07)

- Construído sem dados oficiais (decisão do usuário): formato definido, importação e consulta prontas; nenhum número em data/.
- Fatores, GWP e misturas são referência global, não dado de cliente: tabelas sem tenant_id (justificadas no registro de cobertura), sem RLS, só leitura para o app_user.
- Inserção só pelo papel app_curadoria, usado pela linha de comando: uma falha na API não consegue plantar fator.
- Aprovação por lote (uma decisão por arquivo importado, gravada uma única vez), com o nome de quem conferiu. Rejeição exige justificativa.
- Bitemporal com o tempo de conhecimento vindo da aprovação: o cálculo guarda o instante e refaz a mesma conta depois. Esse instante vem do relógio do banco (instante_do_banco), porque o relógio do host divergiu do do banco no WSL2 e escondeu um fator recém-aprovado no teste.
- Linha idêntica (mesmo hash canônico, que ignora a escala decimal e inclui a fonte) a uma versão já aprovada não é gravada de novo; o mesmo arquivo não é importado duas vezes.
- Entre versões que cobrem a mesma data, vale a aprovada por último. Na mistura, cada componente segue a mesma regra; por isso a vigência de uma mistura que muda de composição precisa ser fechada no arquivo.
- Unidade do fator como "massa/atividade", conferida pelo registro pint único, com Decimal.

## 2026-11-23: coleta e evidências (P08)

- Construído sem as planilhas oficiais nem a regra de materialidade (data/templates/coleta/ vazia), como no P07, por decisão do usuário: o formato da planilha foi definido aqui e a materialidade é decidida fonte a fonte, com justificativa obrigatória para deixar uma fonte de fora. Nenhum limite percentual de materialidade foi inventado.
- O inventário nasce no P08 (antes do fechamento), porque a coleta precisa de ano, abordagem e GWP: um por empresa e ano, só no estado "em coleta". Fechar e reapresentar ficam para o P11, que amplia o CHECK de estado. A abordagem é fixada na abertura.
- Categorias só dos escopos 1 e 2, como estrutura (códigos no formato da coluna categoria de data/fatores). O escopo 3 entra no P17. Energia elétrica é sempre mensal, porque o fator do SIN é mensal (P10).
- Lançamento só de inserção, em versões por fonte e período; decisão (aprovado ou devolvido) gravada uma única vez por versão. Corrigir um dado aprovado cria versão nova, que volta a pedir aprovação.
- Segregação de funções no banco: quem lançou não decide, e só a versão mais recente é decidida (trigger lancamento_decisao_checar). Pode travar um cliente de uma pessoa só; nesse caso, a saída é convidar um segundo usuário. A trigger lancamento_checar recusa dado fora da periodicidade, em fonte desativada ou em inventário fora da coleta.
- Chaves compostas com empresa_id (inventário, unidade e fonte) impedem uma fonte de misturar unidade de uma empresa com inventário de outra. Para isso a unidade ganhou UNIQUE (tenant_id, unidade_id, empresa_id).
- Progresso: "Coleta" conta períodos com dado aprovado; "Evidências" conta períodos com evidência do período ou de todos os períodos da fonte. Só fontes ativas e materiais contam.
- Evidência: tipo pelo conteúdo (nunca pelo nome nem pelo Content-Type do navegador), extensão coerente com o conteúdo, macro recusada pela extensão e pelo vbaProject.bin no pacote. ClamAV síncrono no envio, com falha fechada: sem veredito, nada é guardado. Cliente INSTREAM próprio (asyncio), sem biblioteca extra.
- S3 pelo boto3 (em thread, para não travar o event loop), com Object Lock por objeto e ChecksumSHA256: o próprio S3 recusa conteúdo que não bate com o hash. Em produção a configuração exige COMPLIANCE; em dev, GOVERNANCE, para o bucket local poder ser limpo. A retenção padrão de 1825 dias (5 anos) é configuração provisória: o prazo real depende de exigência do Programa e da LGPD e precisa ser confirmado.
- A chave do objeto leva só identificadores; o nome do arquivo fica no banco. Ordem de gravação: S3 antes do banco, para sobrar no máximo objeto sem registro (retido), nunca registro sem objeto.
- Planilha de coleta em CSV (não XLSX) até os modelos oficiais existirem: lê UTF-8 e Windows-1252 (o Excel em pt-BR salva assim), importação tudo ou nada, células do modelo neutralizadas contra fórmula. O CSV importado também fica guardado no S3, com hash.
- Texto livre (observação, motivo, justificativa, nome de arquivo) não vai para a trilha de auditoria: pode ter dado pessoal. A trilha guarda identificadores, período, versão, hash e tamanho.
- Os testes do portal usam S3 e ClamAV substitutos em memória; o S3 de verdade é testado contra um MinIO com Object Lock (tests/integration/test_armazenamento_minio.py) e o protocolo do ClamAV contra um clamd falso por TCP.
- Tabela numérica ganhou a opção exato (casas que o número tem, de 2 a 6): 0,0015 t não pode aparecer como 0,00.

## 2026-11-23: escopo 1 (P09)

- Construído com dados fictícios (decisão do usuário): data/ continua sem GWP, fatores e misturas oficiais. Os testes usam tests/dados_ficticios/calculo_*.csv, com o conjunto de GWP TESTE_CALC_100, para não se misturar com os lotes de teste do P07.
- Método fator_direto 1.0.0 (decisão do usuário): quantidade × fator por gás × GWP × consolidação, com a mistura pela data da compra. Sem densidade nem poder calorífico: grandezas diferentes viram pendência. A cadeia da ferramenta oficial (massa, energia, fator por energia) entra quando a planilha estiver em data/templates (P13), como método novo.
- A fonte de emissão ganhou "atividade", o código do combustível ou da atividade, escolhido entre os fatores e misturas aprovados. Pode ficar para depois; vira pendência no cálculo.
- O fator é procurado pela categoria da fonte e pelo código. A validação dos arquivos e a chave única de fator_versao passaram a incluir a categoria, porque o mesmo componente tem fatores diferentes em combustão móvel e estacionária.
- Só entra dado aprovado (a versão mais recente com decisão aprovada). Todo o resto vira pendência explícita (falta_dado, sem_atividade, sem_fator, unidade_incompativel, sem_gwp): nada é contado como zero. Se um componente da mistura não tem fator, o lançamento inteiro fica pendente, para não subestimar.
- Data de referência sem data da compra: o último dia do período.
- O percentual de consolidação é lido da unidade no momento do cálculo e gravado em cada linha, porque a unidade pode mudar depois.
- Execução só de inserção (calculo, calculo_linha, calculo_pendencia), com a entrada canônica em jsonb, o hash da entrada e o do resultado. Conferir refaz a conta a partir da entrada guardada: prova que o resultado é reproduzível e que a entrada não foi trocada. Reconstruir a entrada do zero não seria possível, porque fonte e unidade mudam.
- Precisão Decimal de 50 dígitos, sem arredondar no caminho; a tela mostra 3 casas; a memória em CSV sai sem arredondar e sem separador de milhar.
- O cálculo roda na requisição, com a conexão asyncpg da própria sessão (contexto de tenant já aplicado), pela mesma função da tarefa Celery calculo.calcular_inventario. Um volume que torne o cálculo lento muda isso para fila.
- Só o escopo 1. Fontes do escopo 2 ficam fora do cálculo até o P10 (fator do SIN mensal), sem pendência.

## 2026-11-23: escopo 2 e paridade (P10)

- Construído com dados fictícios (decisão do usuário, "continue" depois da pergunta): data/fatores/sin/ e data/paridade/ continuam vazias.
- Escopo 2 pela abordagem de localização, com o mesmo método: consumo × fator do SIN do mês. O fator do SIN entra como fator comum, com vigência mensal, sem tabela própria. A abordagem por escolha de compra (instrumentos contratuais) não entra: é opcional no Programa e pede um insumo que ainda não existe.
- No escopo 2, a data de referência é sempre o último dia do mês do consumo, nunca a data da compra.
- Método fator_direto 1.1.0: inclui o escopo 2 e grava o total dele separado (calculo.escopo2_tco2e, migração 0008). A conta pura não mudou; por isso as execuções 1.0.0 continuam conferíveis (VERSOES_COMPATIVEIS), com o hash calculado do jeito antigo.
- A montagem da entrada saiu do serviço de banco para app/modules/calculo/montagem.py, com a referência por interface (ReferenciaDoBanco e ReferenciaDoCaso). A paridade passa pela mesma montagem e pelo mesmo método que a produção, não por uma cópia.
- Formato do caso de paridade definido aqui (data/README.md). Tolerância de 0,001 tCO2e por total, em configuração (PARIDADE_TOLERANCIA_TCO2E): é o arredondamento de 3 casas da tela. O valor é provisório até os casos oficiais mostrarem com quantas casas a ferramenta trabalha; mudar exige decisão registrada aqui.
- Sem caso oficial, o teste de paridade oficial fica pulado com o motivo, e um caso fictício prova que o comparador funciona e sabe falhar.
- O leitor de CSV do P07 virou público (ler_tabela), para os casos de paridade usarem o mesmo formato e as mesmas mensagens de erro.

## 2026-11-23: fechamento (P11)

- Decisões tomadas por mim, com o usuário seguindo ("continue") sem responder às três perguntas; todas podem ser revistas:
  - fecha só sem nenhuma pendência; a saída para uma fonte sem dado é tirá-la da materialidade, com justificativa. Período sem evidência é aviso, não bloqueio;
  - fecham administrador e gestor (os perfis que veem o tenant inteiro);
  - reapresentação reabre o mesmo inventário, com motivo obrigatório; o fechamento anterior fica guardado e o próximo é a versão seguinte. Não há cópia do inventário: como tudo é só inserção, a história completa já fica no mesmo registro.
- "Cálculo atualizado" é verificado pelo hash: a entrada montada agora, no mesmo instante de conhecimento do cálculo, precisa ter o mesmo hash da entrada gravada. Não depende de datas de atualização, que podem não cobrir toda mudança.
- O fechamento exige também a conferência (refazer o cálculo pela entrada guardada).
- Hash do fechamento: hash do resultado do cálculo, lançamentos usados e evidências (id e SHA-256), com a versão do fechamento.
- No banco: o estado só muda com o evento correspondente gravado na mesma transação (comparando txid_current()), e um inventário fechado não recebe nada (triggers em fonte, decisão, evidência, planilha e cálculo; o lançamento já era barrado desde a 0006). Assim nem um caminho fora da API fecha, reabre ou altera um inventário fechado sem deixar o evento.
- A confirmação por digitação passou a enviar o texto (campo confirmacao), e o servidor confere. Antes, só o navegador conferia.
- registrado_em dos eventos usa clock_timestamp(), para a ordem valer até dentro da mesma transação.
- A conferência trata uma entrada guardada ilegível como "não confere", em vez de derrubar a tela.

## 2026-11-23: verificador (P12)

- Decisões tomadas por mim (o usuário seguiu com "continue"), revisáveis: só o administrador concede e revoga; a concessão cobre um inventário fechado; prazo padrão de 90 dias, até 365, revogável a qualquer momento.
- O verificador não é membro do cliente: é uma pessoa (usuario global) com uma concessão aceita. Ele entra por um link de uso único, com o mesmo formato e as mesmas garantias do convite (segredo de 32 bytes, só o SHA-256 no banco, um único erro CL001 para todo motivo de recusa, e-mail conferido pelo provedor).
- Duas funções novas do app_identidade (SECURITY DEFINER), revisadas na migração 0010: identidade_aceitar_concessao e identidade_concessoes. A lista fechada de funções do papel foi atualizada no teste.
- O limite mora no banco, como pedido no P02: contexto app.concessao_id, SET LOCAL ROLE app_verificador na mesma transação e uma política restritiva TO app_verificador em toda tabela de tenant (o verificador vê o inventário da concessão e o que pende dele, a empresa, as unidades e o grupo; nada de membros, convites, trilha ou outras concessões). Concessão inválida, vencida, revogada ou de outra pessoa não abre nada.
- O contexto do verificador exige escopo de empresas com exatamente a empresa da concessão e o usuário; o ContextoTenant recusa outro formato.
- O acesso do verificador é relido no banco a cada requisição (como o dos membros): revogar corta na hora.
- O teste genérico do verificador na suíte de vazamento mudou: antes ele lia o tenant inteiro (o P02 deixou o limite por concessão para o P12); agora cada tabela é visível só no recorte da concessão, ou invisível. O teste de cobertura exige a política do verificador em toda tabela nova.
- O verificador lê a referência global (fatores, GWP, misturas) para conferir a memória de cálculo.
- O acesso do verificador às telas não vai para a trilha: o papel dele só lê. A trilha registra concessão criada, aceita e revogada.

## 2026-11-23: portal 1a (P14)

- O P13 (ferramenta oficial) foi pulado: depende da planilha oficial em data/templates, que não existe, e o CLAUDE.md proíbe inventar células dela. O usuário seguiu com "continue"; escolhi o P14 pelo que o progresso do P04 já marcava como pendente para ele (gestão de usuários e limite de tentativas de login). A parte do P14 que usa os casos de paridade fica para quando eles existirem.
- Usuários e acessos em tela própria, só do administrador, com link no menu do usuário (topo) em vez do menu principal, que segue a ordem do trabalho. As regras são as mesmas da API do P04 (mesmas funções).
- Componente novo campo_opcoes (várias caixas com o mesmo nome) para escolher empresas, primeiro na /ui.
- Início com a jornada da empresa em foco (organização, coleta, resultados, fechamento, verificação): a primeira etapa não feita é o próximo passo.
- Limite de tentativas de login por IP, com janela fixa no Redis (LOGIN_LIMITE_TENTATIVAS, padrão 20, e LOGIN_JANELA_MIN, padrão 10). Conta o início de cada login (entrar, convite, concessão); passou do limite, 429 com o tempo de espera, que não revela nada sobre contas. A chave guarda só o hash do IP. Atrás de proxy (P25), o IP real precisará vir do cabeçalho confiável do balanceador.

## 2026-11-23: metas (P18)

- Escolhido depois do P14 por não depender de arquivo oficial (P15 precisa dos XSD da NF-e e do CT-e; P16 e P17 ficaram como alternativas). O usuário seguiu com "continue".
- Meta de redução percentual, por empresa, de um ou dois escopos (1, 2 ou 1+2), do ano-base ao ano-alvo. Só metas absolutas; intensidade (por produção ou receita) fica para quando houver o indicador de atividade.
- O ano-base é um inventário fechado. O valor-base é copiado do cálculo daquele fechamento e guardado na meta, com o id do cálculo: uma reapresentação posterior não muda a meta em silêncio.
- Nenhum percentual, ano ou referência é sugerido pelo sistema (CLAUDE.md: nada de inventar números). A meta é a que o cliente assume.
- Trajetória linear do ano-base ao ano-alvo. Realizado de cada ano: o cálculo do fechamento quando fechado; senão, o último cálculo, marcado como preliminar.
- Ações do plano com redução estimada informada pelo cliente: estimativa, nunca cálculo, nunca entra no inventário. A tela compara a soma das ações não canceladas com a redução da meta.
- Meta não se apaga: arquiva. O verificador não vê metas nem ações (política restritiva com false).
- Criar e arquivar metas: administrador e gestor. Ações: também o responsável.

## 2026-11-23: escopo 3 (P17)

- Construído com dados fictícios, pelo mesmo motivo dos P07 a P10 (data/ sem fatores oficiais). O usuário seguiu com "continue".
- As 15 categorias do escopo 3 do GHG Protocol entram no catálogo como estrutura (códigos escopo3.cat01_bens_servicos a escopo3.cat15_investimentos); nenhum fator mora no código.
- Mesmo método (fator direto): quantidade × fator × GWP × consolidação. Cobre fatores por atividade (km, t·km, kg de resíduo) e por valor gasto (kg/BRL). Métodos específicos de categoria (híbrido, por fornecedor, por média de uso de produto) ficam para quando houver insumo oficial e pedido.
- O registro do pint ganhou a moeda (BRL) como grandeza própria, sem conversão entre moedas: câmbio não é unidade.
- No escopo 3, a data de referência segue a do escopo 1 (data da compra; sem ela, o fim do período).
- Método fator_direto 1.2.0 com total do escopo 3 separado (calculo.escopo3_tco2e, migração 0012). As versões 1.0.0 e 1.1.0 continuam conferíveis, cada uma com o hash do jeito dela.
- Metas continuam com os escopos 1 e 2 (1, 2 ou 1+2). Meta de escopo 3 entra se for pedida.

## 2026-11-23: IA (P16)

- A IA só sugere, em dois lugares: categoria e atividade de uma fonte nova (a partir da descrição e da unidade) e opinião sobre um valor fora do padrão para quem vai decidir o dado. Ela não preenche campo, não grava dado, não calcula e não aprova: a pessoa lê e decide.
- Saída estruturada (output_config.format com JSON Schema): a categoria e a atividade só podem ser uma das opções que existem (catálogo e fatores aprovados), e o servidor confere de novo antes de mostrar.
- Modelo só por ANTHROPIC_MODELO (CLAUDE.md); sem ele ou sem ANTHROPIC_API_KEY, a IA fica desligada e as telas nem mostram os botões.
- Esforço baixo (classificação curta), raciocínio adaptativo no padrão do modelo, até 4.096 tokens, 30 s de limite (IA_TEMPO_LIMITE_S) e 2 novas tentativas do SDK.
- Recusa por política: o servidor repete o pedido num modelo de reserva (fallbacks "default"), sem nome de modelo no código. IA_FALLBACKS vazio desliga, para modelos que não aceitam o parâmetro. Recusa final, resposta cortada, JSON inválido e erro de API ou de rede viram aviso na tela; nada quebra.
- Sem cache de prompt: o prompt de sistema é curto demais para o mínimo cacheável.
- Vai para a Anthropic só o necessário: descrição e unidade da fonte, e os números dos períodos. Nenhum nome, e-mail ou documento. Nem o texto enviado nem o recebido vão para log ou trilha; a trilha registra só que a IA foi consultada (ia.consultada, com o uso e a empresa).
- Pendente para o P23 (LGPD): decidir se cada cliente precisa ligar a IA explicitamente (opt-in por tenant), já que os dados saem para um operador externo.

## 2026-11-23: integridade (P20)

- Escolhido por não depender de conta ou arquivo externo (o usuário seguiu com "continue").
- Verificação por tenant que não confia em quem gravou: cadeia da trilha (formato v1, em Python), cálculos refeitos pela entrada guardada, hash do fechamento atual de cada inventário fechado, e SHA-256 de cada evidência e planilha relida do S3.
- Só o fechamento atual de cada inventário é conferido: fechamentos anteriores a uma reapresentação foram feitos sobre dados que a reapresentação mudou de propósito; eles continuam guardados, com o hash da época.
- Resultado só por inserção (integridade_verificacao), com CHECK que amarra "íntegra" à lista de falhas vazia. RLS só de tenant; o verificador externo não vê.
- Agendamento diário às 3h (America/Sao_Paulo) no beat. O disparo é a única tarefa global: lista os tenants pela função identidade_tenants (do app_identidade, só ids, revisada na migração 0013) e enfileira uma verificação por tenant com tenant_id explícito.
- A verificação pedida na tela roda na requisição, pela mesma função do worker.
- Falha não é corrigida automaticamente: é incidente, com runbook (docs/runbooks/integridade.md), log de erro sem dado pessoal e destaque na tela.

## 2026-11-23: painel (P24)

- Escolhido por não depender de conta ou arquivo externo (o usuário seguiu com "continue").
- Uma tela com todas as empresas visíveis (o RLS limita o responsável e o colaborador às empresas deles), com filtro por grupo; grupo desconhecido cai em "todos".
- O número de cada empresa vem do inventário mais recente: do cálculo do fechamento atual, quando fechado; senão, do último cálculo, marcado como preliminar. Empresa sem cálculo aparece sem número, nunca com zero; na série anual só entram inventários com cálculo, e o ano com algum número preliminar é marcado.
- Gráficos de barras empilhadas por escopo (ECharts), sempre com a tabela equivalente.
- O menu ganhou "Painel" no fim, depois de Metas: acompanhar o consolidado vem depois do trabalho de cada empresa. O teste da ordem do menu foi atualizado.

## 2026-11-23: campanhas de coleta (P21)

- Escolhido por não depender de conta ou arquivo externo (o usuário seguiu com "continue"). Escopo definido por mim: campanha é o prazo de coleta de um inventário, com aviso à equipe e lembretes por e-mail.
- Uma campanha aberta por inventário de cada vez (índice único parcial); administrador e gestor abrem e encerram. Prazo depois de hoje; recado opcional.
- Destinatários: responsáveis e colaboradores ativos atribuídos à empresa; sem ninguém atribuído, a gestão. O e-mail leva só a lista do que falta (fonte, período e situação), sem números.
- Lembretes a 7, 3 e 1 dia do prazo e no vencimento, cada tipo uma vez só: o registro do envio (só inserção, chave por campanha e tipo) é gravado antes do e-mail, e a chave única impede aviso repetido mesmo com duas rodadas ao mesmo tempo. Se o envio falhar, a transação desfaz o registro e a próxima rodada tenta de novo.
- A rodada diária (beat, 8h) encerra sozinha a campanha cuja coleta ficou completa ou cujo inventário foi fechado.
- O disparo diário reusa identidade_tenants (só ids), como o da integridade; é a segunda tarefa global justificada.

## 2026-11-24: LGPD (P23)

- IA por cliente (opt-in): a IA envia descrição, unidade e números a um operador externo, então só funciona no cliente cujo administrador a ligou. Desligada por padrão, inclusive para clientes antigos. Resolve a pendência do P16.
- Anonimização, não exclusão física (decisão do usuário): nome e e-mail viram código, vínculos de login são removidos, acessos, convites e concessões se encerram. Trilha e dados aprovados ficam com o id, que já não identifica ninguém. Preserva "nada some" e a cadeia de hash da trilha.
- O pedido é da pessoa, na tela Meus dados; a execução é da plataforma, pela linha de comando, porque a pessoa existe em vários clientes e a ação é irreversível. Prazo operacional de 15 dias.
- lgpd_pedido é global (a pessoa está acima dos clientes), com RLS e FORCE sem política: só as funções do app_identidade leem e gravam.
- Dados pessoais dentro de evidências e lançamentos são do cliente (controlador); a plataforma não mexe neles por pedido do titular.
- Retenção das evidências (1825 dias) segue provisória: o Object Lock prevalece sobre o pedido de exclusão até a confirmação do prazo real.

## 2026-11-24: relatório assinado (P19)

- Cada cliente assina com o próprio certificado A1 (decisão do usuário), por empresa: o e-CNPJ é da empresa, e um cliente pode ter várias. Um certificado ativo por empresa; só o administrador envia, troca ou desativa.
- O .pfx é aberto com a senha só na hora do envio e guardado cifrado, já sem senha (cifra envelope de app/core/keys.py, contexto com tenant, empresa, certificado e finalidade). A senha não é guardada em lugar nenhum.
- Conferências do certificado: senha, chave privada que combina com o certificado, RSA ou curva elíptica, validade, uso de chave que permite assinar e CNPJ ICP-Brasil (OID 2.16.76.1.3.3) igual ao da empresa. Certificado sem CNPJ é aceito só fora de produção (para testes com certificado autoassinado); em produção, só e-CNPJ.
- Desativar apaga a chave cifrada (a linha fica, com os dados públicos, porque os relatórios antigos apontam para ela). Trocar o certificado desativa o anterior. O banco só deixa o app desativar (privilégio por coluna e trigger), nunca alterar ou apagar.
- O relatório retrata um fechamento: versão, hash do fechamento e os números do cálculo daquele fechamento. Nada é recalculado. Fica só por inserção (relatorio_inventario), com o SHA-256 do PDF e o objeto no S3 com Object Lock; pode haver vários por fechamento (por exemplo, depois de trocar o certificado), e a tela marca qual retrata o fechamento atual.
- O texto do relatório diz que a plataforma calcula e organiza, não certifica, e que a assinatura atesta autoria e integridade do documento, não as emissões.
- PDF pelo Chromium do Playwright, sem JavaScript e sem rede (todas as requisições abortadas); o tema.css vai embutido no HTML, com regras próprias de .cl-relatorio. A imagem Docker passa a instalar o Chromium.
- Assinatura PAdES B-B (SHA-256, subfilter ETSI.CAdES.detached) com pyHanko, num campo novo, por atualização incremental. Sem carimbo do tempo nem dados de revogação: PAdES B-T/LTA depende de escolher uma TSA (ICP-Brasil ou outra), e a validação da cadeia precisa das raízes ICP-Brasil em data/. Pendente de decisão.
- Geração na própria requisição (como a verificação de integridade pedida na tela): leva alguns segundos. Se ficar lento com inventários grandes, vai para o worker.
- O verificador ainda não vê os relatórios (política restritiva). Dar acesso a ele pelo painel da concessão é o passo seguinte natural.
- Sem chaves configuradas (PROVEDOR_CHAVES diferente de local até o KMS do P25, ou CHAVES_LOCAIS vazio), a tela avisa e não aceita certificado nem gera relatório.
- Com "Relatórios" ligado, o menu passou a ter 9 itens e empurrou o nome do usuário e o seletor de empresa para fora da tela em 1280 px (o axe-core acusou contraste no texto cortado). O topo agora vira botão de menu abaixo de 1200 px (navbar-expand-xl) e, entre 1200 e 1599 px, os itens do menu ficam sem ícone e mais juntos. Conferido por captura em 1100, 1200, 1280, 1440 e 600 px.
- O verificador vê e baixa o relatório assinado do inventário da concessão, no painel dele (migração 0017). Do certificado, vê só as colunas públicas do que assinou esses relatórios: a coluna chave_cifrada sai do SELECT dele por privilégio de coluna, e a suíte de vazamento confere a recusa.

## 2026-11-24: CI e imagem (P26, parte sem conta externa)

- O CI segue em GitHub Actions com make check. Entram: cancelamento de rodada antiga, limite de tempo, dependências exatas do uv.lock, histórico completo para o gitleaks (antes só via o último commit) e cache do Chromium.
- Job novo "imagem": monta a imagem de produção e gera um PDF dentro dela, com o usuário do app. Nada é publicado: registro de imagem e deploy dependem da conta AWS (P25).
- Imagem: código e dependências pertencem ao root e o usuário do app só lê; o que precisa escrever vai para /tmp (agenda do beat, perfil do Chromium). Só o Chromium headless (--only-shell), que basta para PDF.
- Fora por enquanto: Dependabot (abre PRs sozinho; decidir com o usuário) e ações fixadas por hash.

## 2026-11-24: backup e restauração locais (P27, parte local)

- Tudo roda pela pilha do docker compose, com os segredos dentro dos containers: pg_dump e pg_restore no container do Postgres, mc no do MinIO, a verificação no do app. A linha de comando do host não recebe senha.
- Backup cifrado arquivo a arquivo com a chave da plataforma (contexto com o id do backup e o nome do arquivo), porque leva dado pessoal; sem a chave não abre. Na nuvem, troca por backup gerenciado com KMS.
- O manifesto guarda a revisão do Alembic e o SHA-256 de cada original; o ensaio para em qualquer diferença.
- Restauração sempre ensaiada numa cópia descartável no mesmo servidor (<banco>_ensaio e bucket ensaio-<id> sem Object Lock), com a verificação de integridade de todos os tenants. Diferença de contagem de linhas é aviso (o app pode ter gravado depois do backup).
- Objetos restaurados ganham novos ids de versão. Em vez de reescrever versao_objeto (tabelas só de inserção), a leitura cai na versão atual da mesma chave quando a gravada não existe: cada chave é escrita uma vez só e o SHA-256 é conferido antes de usar o conteúdo.
- Os papéis do banco não vão no backup (são do servidor): numa máquina nova, a pilha sobe e cria os papéis antes da restauração.
- A verificação de integridade passou a incluir os relatórios assinados, que tinham ficado de fora no P19.

## 2026-11-24: KMS (preparo do P25)

- ProvedorChavesKMS em app/core/keys.py: Encrypt e Decrypt da chave de dados no AWS KMS, com o contexto canônico como EncryptionContext (o KMS amarra e registra). O envelope guarda o ARN devolvido, para a rotação continuar abrindo os antigos. Erros de rede ou permissão viram ErroChave: nunca se cifra sem KMS.
- Emulador local: local-kms (MIT, 36 MB) em vez do LocalStack, por licença e peso. Serviço "kms" no docker compose, sem porta no host, chave fixa de dev em infra/kms/seed.yaml (fixa porque o local-kms não guarda estado e envelopes precisam sobreviver ao reinício).
- Configuração: KMS_CHAVE_ID, KMS_REGIAO (padrão sa-east-1) e KMS_ENDPOINT (só dev). PROVEDOR_CHAVES aceita só local ou kms; em produção exige kms e KMS_CHAVE_ID.
- Migração: certificados cifrados com a chave local são reenviados pelos administradores (não há recifragem automática, porque a tabela só permite desativar); backups locais seguem precisando da chave local.
- Chamada ao KMS é síncrona (alguns ms); se pesar, vai para thread como o S3.

## 2026-11-24: ensaio de produção local

- O ensaio usa a mesma imagem de produção e AMBIENTE=producao, com todas as travas; o que muda é só a infraestrutura de apoio (KMS local, MinIO, Caddy com CA interna, Mailpit). Sem login interativo, porque produção só aceita Google e Microsoft.
- Banco só com TLS no ensaio: pg_hba recusa conexão sem TLS pela rede. A URL usa sslmode=require (padrão do libpq); na nuvem, sslmode=verify-full com a CA do banco gerenciado.
- Contêineres do app com raiz só leitura, /tmp em memória, sem capacidades e com no-new-privileges; o ensaio confere cada um por docker inspect.
- Uvicorn confia no X-Forwarded-For de qualquer origem dentro da rede do ensaio (FORWARDED_ALLOW_IPS=*), porque a API não tem porta publicada: só o proxy chega nela. Na nuvem, a lista do balanceador.
- Retenção de 1 dia no ensaio, para o COMPLIANCE do MinIO não prender objetos de teste.

## 2026-11-24: carimbo do tempo

- Carimbo do tempo opcional por configuração (TSA_URL), porque a escolha da autoridade (ICP-Brasil, com contrato) ainda é do usuário. Com TSA, PAdES B-T; sem, B-B, e a tela mostra "Sem carimbo".
- TSA configurada e fora do ar: o relatório não sai. Gerar sem carimbo sem avisar daria um documento mais fraco do que o esperado.
- A hora do carimbo é lida do token embutido (gen_time) e gravada em relatorio_inventario.carimbo_em na mesma inserção.
- B-LT (dados de revogação embutidos) fica para quando as raízes ICP-Brasil estiverem em data/.

## 2026-11-25: demonstração local

- Demonstração completa com referência fictícia (escolha do usuário), só no banco local. Códigos demo.* e conjunto DEMO_FICTICIO_100, com FICTICIO na fonte e na descrição, para não se confundir com AR5_100 nem com fatores oficiais. A referência entra pela curadoria, como qualquer lote.
- O roteiro usa o próprio app (HTTP, login pelo provedor de dev, convite pelo Mailpit), para a demonstração passar pelas mesmas validações, trilha, antivírus e S3 de um uso real. O único atalho é trocar o GWP do inventário da demonstração no banco local, porque a tela não oferece escolha de conjunto.
- O script recusa rodar contra API em produção.
