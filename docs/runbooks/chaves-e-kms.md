# Chaves e KMS

## Provedores

| PROVEDOR_CHAVES | Onde fica a chave mestra | Quando usar |
| --- | --- | --- |
| local | CHAVES_LOCAIS no .env (id:base64, a primeira cifra, todas decifram) | Desenvolvimento e testes |
| kms | AWS KMS (KMS_CHAVE_ID: ARN, id ou alias) | Produção (obrigatório) e ensaio local com o serviço "kms" |

O que é cifrado: a chave privada do certificado A1 de cada empresa e os backups locais. Cada valor ganha uma chave de dados própria, cifrada pela chave mestra com o contexto (tenant, finalidade e ids) amarrado; com o KMS, o contexto vai como EncryptionContext e aparece no CloudTrail.

## Usar o KMS local

No .env: `PROVEDOR_CHAVES=kms`, `CHAVES_LOCAIS=` (vazio), `KMS_CHAVE_ID=alias/carbonledger`, `KMS_ENDPOINT=http://kms:8080`, `AWS_ACCESS_KEY_ID=local`, `AWS_SECRET_ACCESS_KEY=local`. Depois, `docker compose up -d`.

A chave do serviço "kms" é fixa e conhecida (infra/kms/seed.yaml): serve para ensaio, nunca para dado real.

## Produção

- Chave simétrica no AWS KMS, com rotação automática anual; o envelope guarda o ARN, e versões antigas da chave continuam abrindo.
- Política da chave: Encrypt e Decrypt só para o papel da aplicação; administração separada.
- Sem KMS_ENDPOINT e sem AWS_ACCESS_KEY_ID: as credenciais vêm do papel da instância.

## Migração do local para a nuvem

- Certificados A1: envelopes cifrados com a chave local não abrem no KMS. Depois de trocar o provedor, o administrador de cada empresa envia o certificado de novo (o anterior é desativado e a chave apagada). Relatórios já assinados não mudam.
- Backups locais: abrem só com a CHAVES_LOCAIS da época. Guarde-a até não precisar mais deles.

## Problemas

| Sintoma | Causa provável |
| --- | --- |
| "KMS recusou cifrar" | Chave inexistente, sem permissão ou KMS fora do ar |
| "KMS recusou decifrar" | Contexto diferente (envelope de outro tenant) ou envelope cifrado com outra chave |
| App não sobe em produção: "defina KMS_CHAVE_ID" | Falta a variável |
