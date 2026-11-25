# Cobrança

## Como funciona

- Preço por empresa ativa por mês, progressivo por faixas, definido em COBRANCA_TABELA (`ate:preço;...;*:preço`, em reais com vírgula). Sem tabela, a cobrança fica desligada.
- Cada cliente ganha uma assinatura na primeira rodada, em período de teste até a criação do cliente mais COBRANCA_DIAS_TESTE (padrão 30).
- A rodada diária (beat, 6h) de cada cliente marca faturas vencidas, passa a assinatura para "pagamento em atraso" quando uma fatura vence há mais que COBRANCA_DIAS_TOLERANCIA (padrão 15) e, depois do teste, emite a fatura do mês com as empresas ativas no dia (vencimento em COBRANCA_DIAS_VENCIMENTO, padrão 10).
- O gateway confirma o pagamento pelo webhook `POST /cobranca/webhook/<gateway>`, com o token COBRANCA_WEBHOOK_TOKEN. Cada evento é processado uma vez só.
- Tela Cobrança (menu do usuário, só administrador): situação, tabela de preços, empresas ativas, estimativa do mês e faturas com o link de pagamento.

Trilha: `cobranca.fatura_emitida`, `cobranca.fatura_paga`, `cobranca.situacao_alterada`.

## Pagamento em atraso

Com a assinatura em "pagamento em atraso" (fatura vencida além da tolerância), todas as telas do cliente mostram o aviso e ficam bloqueadas só as ações novas: abrir inventário, fechar inventário, gerar relatório assinado e convidar pessoas. Lançar e aprovar dados, consultar, baixar relatórios, o acesso do verificador, a cobrança e a LGPD continuam. O pagamento confirmado libera na hora. Sem COBRANCA_TABELA, nada é bloqueado.

## Gateway simulado (desenvolvimento)

Com COBRANCA_GATEWAY=simulado, o link de pagamento abre uma página do próprio app ("Pagar (simulado)"), que confirma o pagamento como um webhook faria. Nenhum dinheiro se move. Em produção, o simulado é recusado na configuração.

Para testar localmente: defina COBRANCA_TABELA e COBRANCA_WEBHOOK_TOKEN no .env, rode `docker compose up -d` e dispare a rodada de um cliente:

```
docker compose exec worker celery -A app.workers.celery_app call cobranca.rodada_tenant --kwargs '{"tenant_id": "<id do cliente>"}'
```

Durante o período de teste, nada é faturado.

## O que ainda falta

- Gateway Asaas (P22 com conta): cliente, cobrança idempotente pela referência (o id da fatura) e webhook com o token do Asaas.
- Proporcionalidade no primeiro mês e cancelamento pela tela.

## Problemas

| Sintoma | Causa provável |
| --- | --- |
| App não sobe: "defina COBRANCA_WEBHOOK_TOKEN" | Tabela definida sem token do webhook |
| Webhook responde 404 | Token errado, gateway diferente do configurado ou cobrança desconhecida |
| Nenhuma fatura | Cliente em teste, sem empresa ativa ou sem COBRANCA_TABELA |
