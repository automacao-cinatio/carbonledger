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
