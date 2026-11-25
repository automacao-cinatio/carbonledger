# Cobrança

## Como funciona

- Preço por empresa ativa por mês, progressivo por faixas, definido em COBRANCA_TABELA (`ate:preço;...;*:preço`, em reais com vírgula). Sem tabela, a cobrança fica desligada.
- Cada cliente ganha uma assinatura na primeira rodada, em período de teste até a criação do cliente mais COBRANCA_DIAS_TESTE (padrão 30).
- A rodada diária (beat, 6h) de cada cliente marca faturas vencidas, passa a assinatura para "pagamento em atraso" quando uma fatura vence há mais que COBRANCA_DIAS_TOLERANCIA (padrão 15) e, depois do teste, emite a fatura do mês com as empresas ativas no dia (vencimento em COBRANCA_DIAS_VENCIMENTO, padrão 10).
- O gateway confirma o pagamento pelo webhook `POST /cobranca/webhook/<gateway>`, com o token COBRANCA_WEBHOOK_TOKEN. Cada evento é processado uma vez só.
- A rodada também concilia: antes dos vencimentos, pergunta ao gateway pelas faturas em aberto e marca as pagas (trilha `cobranca.fatura_paga` com origem `conciliacao`). Um webhook perdido não deixa ninguém inadimplente por engano.
- O que fala com o gateway roda à parte: se o gateway falha, vencimentos e inadimplência ficam gravados, a fatura do mês não sai e a rodada do dia seguinte tenta de novo (log `cobranca.gateway_falhou`, só com o tenant).
- A assinatura fica travada durante a rodada: salvar os dados de cobrança nesse instante espera a rodada terminar.
- Trocou COBRANCA_GATEWAY (do simulado para o Asaas, por exemplo): na rodada seguinte o cliente é criado de novo no gateway novo (trilha `cobranca.gateway_trocado`).
- Tela Cobrança (menu do usuário, só administrador): situação, tabela de preços, empresas ativas, estimativa do mês, dados de cobrança (razão social, CNPJ e e-mail de quem paga) e faturas com o link de pagamento.
- Os dados de cobrança são do cliente e podem ser de outra empresa que não as do inventário (a controladora, por exemplo). Mudou o pagador: a rodada atualiza o cliente no gateway antes da próxima fatura.

Trilha: `cobranca.fatura_emitida`, `cobranca.fatura_paga`, `cobranca.situacao_alterada`, `cobranca.pagador_alterado` (sem os dados, que podem ter e-mail de pessoa).

## Pagamento em atraso

Com a assinatura em "pagamento em atraso" (fatura vencida além da tolerância), todas as telas do cliente mostram o aviso e ficam bloqueadas só as ações novas: abrir inventário, fechar inventário, gerar relatório assinado e convidar pessoas. Lançar e aprovar dados, consultar, baixar relatórios, o acesso do verificador, a cobrança e a LGPD continuam. O pagamento confirmado libera na hora. Sem COBRANCA_TABELA, nada é bloqueado.

## Gateway simulado (desenvolvimento)

Com COBRANCA_GATEWAY=simulado, o link de pagamento abre uma página do próprio app ("Pagar (simulado)"), que confirma o pagamento como um webhook faria. Nenhum dinheiro se move. Em produção, o simulado é recusado na configuração.

Para testar localmente: defina COBRANCA_TABELA e COBRANCA_WEBHOOK_TOKEN no .env, rode `docker compose up -d` e dispare a rodada de um cliente:

```
docker compose exec worker celery -A app.workers.celery_app call cobranca.rodada_tenant --kwargs '{"tenant_id": "<id do cliente>"}'
```

Durante o período de teste, nada é faturado.

## Asaas

Com COBRANCA_GATEWAY=asaas (implementado pela documentação pública da API v3; ainda não rodado contra o sandbox, que pede conta):

- ASAAS_API_KEY: a chave da conta (sandbox começa com `$aact_hmlg_`, produção com `$aact_prod_`). ASAAS_BASE_URL vazia usa o sandbox (`https://api-sandbox.asaas.com/v3`). Lista permitida: fora de produção só o sandbox; com AMBIENTE=producao só `https://api.asaas.com/v3`. Qualquer outro endereço, inclusive os antigos do Asaas, impede o app de subir.
- Cliente no Asaas: um por cliente da plataforma, com externalReference igual ao id do cliente. Antes de criar, a rodada procura por ele: a API aceita duplicados.
- Cobrança: boleto (`billingType=BOLETO`), que traz o QR Code Pix quando a conta tem Pix; cartão fica de fora. externalReference é o id da fatura e a rodada procura antes de criar: repetir nunca cobra duas vezes. O link de pagamento é o invoiceUrl.
- Sem os dados de cobrança, a fatura não é emitida e a tela Cobrança avisa o administrador.
- Webhook: cadastre no painel do Asaas a URL `https://<domínio>/cobranca/webhook/asaas` com o token de autenticação igual a COBRANCA_WEBHOOK_TOKEN (não use a chave da API como token). Eventos de cobrança: PAYMENT_RECEIVED e PAYMENT_CONFIRMED marcam a fatura como paga; os demais ficam registrados. Evento de cobrança que não é da plataforma recebe 200 e é ignorado, para a fila do Asaas não pausar (ela pausa depois de 15 falhas seguidas).
- Para testar no sandbox: conta de sandbox, chave no .env, COBRANCA_GATEWAY=asaas, rodada de um cliente com dados de cobrança e um webhook apontando para um túnel até a máquina local.

## O que ainda falta

- Rodar contra o sandbox do Asaas (pede conta).
- Estorno e cobrança removida no Asaas (PAYMENT_REFUNDED, PAYMENT_DELETED) só ficam registrados; nada muda na fatura sozinho.
- Proporcionalidade no primeiro mês e cancelamento pela tela.

## Problemas

| Sintoma | Causa provável |
| --- | --- |
| App não sobe: "defina COBRANCA_WEBHOOK_TOKEN" | Tabela definida sem token do webhook |
| Webhook responde 404 | Token errado ou gateway diferente do configurado |
| Webhook responde 200 com `"recebido": false` | Evento autêntico de cobrança que não é da plataforma: ignorado de propósito. Se for de uma fatura que ainda não estava gravada, a conciliação da rodada marca o pagamento |
| Rodada com aviso "o gateway falhou" | Gateway fora do ar ou recusando: ver `cobranca.gateway_falhou` no log do worker; vencimentos e inadimplência seguiram |
| Nenhuma fatura | Cliente em teste, sem empresa ativa, sem COBRANCA_TABELA ou, no Asaas, sem os dados de cobrança |
| App não sobe: "ASAAS_BASE_URL: a URL de produção" | URL de produção fora de produção, ou sandbox em produção |
| Rodada falha com ErroAsaas (HTTP 401 invalid_environment) | Chave de um ambiente com a URL do outro |
