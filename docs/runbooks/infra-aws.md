# Infra na AWS (P25)

O Terraform em infra/terraform descreve a produção em us-east-1: ECS Fargate (api, worker, beat e ClamAV), RDS PostgreSQL 16, ElastiCache Redis, S3 com Object Lock, KMS, Secrets Manager, balanceador com WAF, Route 53, ACM e SES. Nada foi aplicado ainda: falta a conta da AWS.

```
make infra    # formato, validação e Trivy, em container e sem conta (também roda no CI)
```

## O que sobe

| Peça | Como |
| --- | --- |
| Rede | VPC em duas zonas. Sub-redes públicas (balanceador e NAT), de app (tarefas, saída pelo NAT) e de dados (RDS e Redis, sem rota para fora). S3 por endpoint de gateway; KMS, Secrets Manager, ECR e Logs por endpoints de interface. Registro de tráfego da VPC. |
| Entrada | Balanceador com TLS 1.2 ou 1.3 (certificado do ACM), HTTP só redireciona, cabeçalho inválido descartado. WAF com regras gerenciadas da AWS e limite de 2.000 pedidos por IP em 5 minutos. |
| App | Mesma imagem para api (2 tarefas), worker (2) e beat (1, nunca dois). Usuário 10001, raiz só leitura, sem capacidades, /tmp em volume efêmero. Deploy com reversão automática se a tarefa nova não fica saudável. |
| ClamAV | Serviço próprio (2 tarefas), achado pelo app em clamav.carbonledger.interno. |
| Banco | RDS Multi-AZ, TLS obrigatório (rds.force_ssl), cifrado, backup de 35 dias, proteção contra exclusão, senha administrativa gerada e guardada pelo próprio RDS. |
| Redis | Dois nós com troca automática, TLS, senha e cifra em repouso. Sem despejo de chave: mensagem da fila nunca some por falta de memória. |
| Evidências | Bucket com Object Lock e versionamento, cifrado com a chave de dados, só TLS, sem acesso público. O app grava em COMPLIANCE e não tem permissão de apagar. |
| Chaves | Chave mestra do app (KMS_CHAVE_ID), usada só com o contexto "carbonledger", e chave de dados da infra. Rotação anual nas duas. |
| Segredos | Senhas dos papéis do banco, senha do Redis e token do webhook gerados pelo Terraform. Os externos (OIDC, SMTP e os que você acrescentar) nascem vazios. |
| Alarmes | 5xx da API, API sem tarefa saudável, CPU e disco do banco, memória do Redis, num tópico SNS. |

## Antes do primeiro apply

1. Conta da AWS com acesso de administrador só para quem roda o Terraform.
2. Zona do domínio no Route 53 (o id vai em `zona_route53_id`).
3. Bucket do estado do Terraform, criado à mão: versionado, cifrado, sem acesso público. O estado guarda as senhas geradas, então só a equipe de infra lê esse bucket.
4. Copiar `backend.hcl.exemplo` para `backend.hcl` e `producao.tfvars.exemplo` para `producao.tfvars`. Os dois ficam fora do git. Nenhum segredo entra no tfvars.

## Primeira subida

1. Montar e enviar a imagem. Primeiro crie só o repositório: `terraform apply -target=aws_ecr_repository.app -var-file=producao.tfvars`. Depois faça `docker build`, `docker tag` com o SHA do commit e `docker push` para a saída `repositorio_imagem`. O mesmo SHA vai em `imagem_tag`.
2. `terraform init -backend-config=backend.hcl` e `terraform apply -var-file=producao.tfvars`. Os serviços sobem e falham até os passos 3 a 5, e isso é esperado.
3. Gravar os segredos externos listados na saída `segredos_externos`, um por um, sem deixar o valor no histórico:
   `aws secretsmanager put-secret-value --secret-id carbonledger/producao/SMTP_SENHA --secret-string file://valor.txt` (apague o arquivo depois).
   SMTP_USUARIO e SMTP_SENHA são a credencial SMTP do SES: crie um usuário IAM só com `ses:SendRawEmail` e gere a senha SMTP no console do SES.
4. Criar os papéis do banco com a tarefa avulsa `papeis` (comando abaixo). Ela imprime cada papel com login e BYPASSRLS. Só o app_identidade tem bypassrls=True.
5. Migrar com a tarefa avulsa `migrar`.
6. Forçar novo deploy dos serviços: `aws ecs update-service --cluster carbonledger-producao --service api --force-new-deployment` (repita para worker, beat e clamav). Daí em diante, as versões novas saem pelo workflow `deploy` (abaixo).
7. Pedir ao SES a saída do sandbox. Sem isso, só e-mails verificados recebem convite.
8. Conferir no endereço real: /saude com 200, HSTS e CSP nos cabeçalhos, /ui com 404 e login pelo Google ou Microsoft. Depois, criar o primeiro tenant pela tarefa `operacao` (`criar-tenant`).

## Tarefas avulsas

```
REDE=$(terraform output -raw rede_tarefas_avulsas)
aws ecs run-task --cluster carbonledger-producao --launch-type FARGATE \
  --task-definition carbonledger-producao-migrar --network-configuration "$REDE"
```

- `papeis`: `python -m app.core.papeis_banco`. Idempotente: rodar de novo só reafirma atributos e senhas.
- `migrar`: `alembic upgrade head` como app_owner.
- `operacao`: linha de comando da CINATIO. Troque o comando com `--overrides`, por exemplo `{"containerOverrides":[{"name":"operacao","command":["python","-m","app.cli","integridade","verificar"]}]}`.

A saída de cada uma fica no grupo de logs `/carbonledger/producao/tarefas`.

## Deploy de uma versão nova

Pelo workflow `deploy` do GitHub Actions (Actions > deploy > Run workflow), só a partir da main. Ele:

1. monta a imagem e envia ao ECR com a tag do commit;
2. registra nova revisão de migrar, papeis, operacao, api, worker e beat, trocando só a imagem (variáveis, segredos e limites seguem o Terraform);
3. roda a migração e para se ela não sair com 0;
4. atualiza api, worker e beat e falha se o ECS reverteu a versão (circuit breaker).

O GitHub assume o papel `carbonledger-producao-deploy` por OIDC, sem chave guardada, e só a partir do ambiente `producao`. Para configurar:

1. Em Settings > Environments, crie o ambiente `producao`, limite a branch main e, se quiser, exija aprovação de alguém da CINATIO.
2. Grave nele as variáveis (não são segredo) da saída `terraform output deploy_github`: AWS_DEPLOY_ROLE_ARN, AWS_REGION, ECS_CLUSTER, ECS_PREFIXO, ECR_REPOSITORIO, ECS_SUBNETS e ECS_GRUPOS.

O papel de deploy não mexe em rede, banco, segredo nem chave. Mudanças de infra continuam pelo `terraform apply`. Se mudar variável ou limite de uma tarefa, rode o deploy em seguida: os serviços só usam a revisão que o deploy define (o Terraform ignora a revisão em uso depois da criação).

Voltar uma versão: rode o workflow a partir do commit anterior. As migrações não voltam sozinhas; uma migração errada se corrige com outra migração.

## Pontos a confirmar na primeira subida

| Ponto | Como conferir |
| --- | --- |
| A conta administrativa do RDS consegue dar BYPASSRLS ao app_identidade | A tarefa `papeis` falha na hora se não conseguir. Sem isso o login não funciona: abrir chamado na AWS ou rever o desenho das funções de login |
| /tmp gravável com raiz só leitura no Fargate | API, worker e beat ficam saudáveis (o beat grava a agenda em /tmp). O CI já confere o mesmo com Docker |
| Chromium gera PDF no worker | Gerar um relatório de teste |
| WAF sem falso positivo | Com `waf_so_contar = true`, olhar o log `aws-waf-logs-carbonledger-producao` por duas semanas e só então passar para `false` |
| Imagem do ClamAV | Docker Hub limita downloads: espelhe a imagem no ECR e troque `imagem_clamav` |

## Problemas comuns

| Sintoma | O que fazer |
| --- | --- |
| Tarefa não inicia: "ResourceInitializationError ... secret" | Segredo externo ainda vazio: grave o valor (passo 3) |
| API reinicia em laço | Log da API: a trava de produção do app diz o que falta |
| Upload de evidência recusado com aviso de antivírus | Serviço clamav sem tarefa saudável (a primeira carga das assinaturas leva minutos) |
| Alarme "redis-memoria" | Fila parada crescendo: veja se o worker está saudável antes de aumentar `redis_classe` |
