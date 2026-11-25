# Backup e restauração (pilha local)

Vale para a pilha local do docker compose. Na nuvem (P25 e P27), o mesmo papel passa para os backups gerenciados do banco e para a replicação do S3; o ensaio de restauração continua obrigatório.

## O que entra no backup

| Parte | Como | Onde fica |
| --- | --- | --- |
| Banco inteiro | `pg_dump` em formato custom, dentro do container do Postgres | `backups/<id>/banco.dump.cl1` |
| Evidências, planilhas e relatórios | `mc mirror` do bucket, dentro do container do cliente do MinIO | `backups/<id>/objetos/*.cl1` |
| Manifesto | Revisão do banco (Alembic), SHA-256 e tamanho de cada arquivo original | `backups/<id>/manifesto.json` |

Cada arquivo é cifrado com a chave da plataforma (CHAVES_LOCAIS), amarrado ao id do backup e ao próprio nome: um arquivo trocado de lugar não abre. O manifesto não tem dado pessoal (só chaves de objeto com ids e hashes).

**Sem a chave, o backup não abre.** Guarde CHAVES_LOCAIS fora da máquina e fora da pasta de backups.

Os papéis do banco (app_owner, app_user etc.) são do servidor, não do banco: o backup não os leva. Numa máquina nova, suba a pilha (`make up`), que cria os papéis, antes de restaurar.

## Rotina

```
make backup              # cria backups/<id> e mantém os 7 mais novos
make ensaio-restauracao  # restaura o mais novo numa cópia descartável e confere
uv run python scripts/backup.py listar
```

Rode o ensaio depois de cada backup importante e pelo menos uma vez por mês. Backup que nunca foi restaurado não conta como backup.

## O que o ensaio confere

1. Decifra cada arquivo e confere o SHA-256 contra o manifesto; arquivo a mais, a menos ou adulterado para tudo.
2. Restaura o banco em `<banco>_ensaio`, no mesmo servidor, com as permissões de banco do `infra/sql/papeis.sql`, e confere a revisão do Alembic.
3. Compara a contagem de linhas de cada tabela com o banco de origem. Diferença é aviso, não falha: o app pode ter gravado algo depois do backup.
4. Envia os objetos para um bucket `ensaio-<id>` (sem Object Lock, para poder ser apagado).
5. Roda a verificação de integridade de todos os tenants na cópia (`python -m app.cli integridade verificar --banco ... --bucket ...`): trilha, cálculos, fechamentos e o SHA-256 de cada evidência, planilha e relatório.
6. Apaga o banco e o bucket do ensaio (salvo `--manter-copia`).

Sai com código 1 se algo não bater. Os objetos restaurados ganham novos ids de versão no bucket; o app lê a versão atual da mesma chave quando a versão gravada não existe e sempre confere o SHA-256, então a cópia funciona.

## Restaurar de verdade (perda do banco ou do bucket)

1. Pare a aplicação: `docker compose stop api worker beat`.
2. Rode o ensaio com `--manter-copia` e confira que deu certo.
3. Troque o banco: renomeie o atual (se existir) e o `<banco>_ensaio` para o nome do banco (`ALTER DATABASE ... RENAME TO ...`, como superusuário, sem conexões abertas).
4. Objetos: aponte S3_BUCKET_EVIDENCIAS para o bucket do ensaio ou espelhe de volta para o bucket original (com Object Lock).
5. Suba a aplicação, rode `python -m app.cli integridade verificar` e registre o incidente.

Tudo o que foi gravado depois do backup se perde: avise os clientes afetados.

## Problemas comuns

| Sintoma | O que fazer |
| --- | --- |
| "sem chave da plataforma" | Defina CHAVES_LOCAIS no .env, a mesma usada quando o backup foi feito |
| "nao abre" num arquivo | Chave errada, arquivo corrompido ou trocado: use outro backup |
| Verificação falha com "No module named" | A imagem do app está velha: `docker compose build api` e `docker compose up -d` |
| "relation ... does not exist" na verificação | O banco de origem está numa revisão antiga: `make migrate` antes do backup |
