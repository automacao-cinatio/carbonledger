# Papéis do banco

## Desenvolvimento

Na primeira subida do volume do Postgres, infra/sql/initdb.sh roda infra/sql/papeis.sql e define as senhas de APP_OWNER_SENHA e APP_USER_SENHA. Depois, make migrate aplica as migrações como app_owner.

Trocar uma senha depois da criação do volume exige ALTER ROLE como superusuário, porque o initdb não roda de novo.

## Produção (RDS)

A tarefa avulsa `papeis` do ECS (docs/runbooks/infra-aws.md) roda `python -m app.core.papeis_banco`: conecta com a conta administrativa do RDS, por rede privada e com TLS, roda infra/sql/papeis.sql e define as senhas de app_owner, app_user e app_curadoria a partir do Secrets Manager. É idempotente. As senhas nunca passam por terminal nem por histórico.

A aplicação recebe só DATABASE_URL (app_user); a tarefa `migrar` recebe só MIGRACAO_DATABASE_URL (app_owner); a tarefa `operacao` recebe também CURADORIA_DATABASE_URL. Os testes de isolamento criam os papéis com o mesmo código.

## Conferência rápida

```sql
SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
FROM pg_roles WHERE rolname LIKE 'app_%';
```

Nenhum dos três pode ter rolsuper ou rolbypassrls; app_verificador não tem login.
