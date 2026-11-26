# Ensaio de produção local

Sobe a imagem de produção com `AMBIENTE=producao` e todas as travas ligadas, numa pilha separada da de desenvolvimento (projeto `carbonledger-ensaio`, volumes próprios), e confere a lista de entrada no ar com evidência.

```
make ensaio-producao         # monta, sobe, espera ficar saudável e confere
uv run python scripts/ensaio_producao.py conferir   # só confere, com a pilha de pé
make ensaio-producao-parar   # derruba e apaga os volumes do ensaio
```

Endereço: https://localhost:8443 (o navegador avisa que não confia no certificado: é a CA interna do Caddy).

## O que é conferido

| Item | Evidência |
| --- | --- |
| HTTPS com HSTS, CSP e nosniff | Resposta de /saude pelo proxy |
| HTTP sem TLS recusado | Pedido sem TLS na porta do proxy |
| Galeria /ui e login de dev ausentes | /ui responde 404; a tela de entrar não oferece o provedor de dev |
| Banco só com TLS | Conexão do app_user aparece com TLS em pg_stat_ssl; conexão sem TLS é recusada pelo pg_hba |
| Postgres sem porta no host | docker inspect |
| API, worker e beat endurecidos | Usuário sem root, raiz só leitura, capacidades removidas, no-new-privileges |
| API, worker e beat saudáveis | Healthchecks do docker |
| Banco na última migração | alembic_version contra a última migração do código |
| Cifra pelo KMS | Cifra e decifra pelo provedor KMS de dentro da API |
| Object Lock em COMPLIANCE | Objeto gravado com retenção COMPLIANCE; apagar a versão é recusado |
| PDF | Chromium gera PDF dentro do worker (onde o relatório sai), com raiz só leitura |
| Antivírus | ClamAV barra a assinatura de teste EICAR |

## Diferenças para a nuvem

- KMS local com chave fixa de desenvolvimento (infra/kms/seed.yaml) no lugar do AWS KMS.
- MinIO no lugar do S3; retenção de 1 dia no ensaio (em produção, a configurada). Na AWS, S3_ENDPOINT fica vazio e as credenciais vêm do papel da tarefa (docs/runbooks/infra-aws.md).
- Caddy com CA interna no lugar do balanceador com certificado público.
- Certificado do Postgres gerado a cada subida; na nuvem, o do RDS (sslmode=require: TLS obrigatório, sem conferir a cadeia, porque a imagem ainda não traz a CA do RDS).
- Sem login interativo: em produção só Google e Microsoft, que pedem contas reais.
- Mailpit como SMTP.

## Problemas comuns

| Sintoma | O que fazer |
| --- | --- |
| Espera passa de 15 minutos | O ClamAV ainda baixa as assinaturas na primeira subida: rode `conferir` depois |
| API não sobe com erro de configuração | Leia `docker compose -p carbonledger-ensaio logs api`: a trava de produção diz o que falta |
| Item "Banco só com TLS" falha | Confira o pg_hba.conf do ensaio e se a DATABASE_URL tem sslmode=require |
