# Papéis do banco

## Desenvolvimento

Na primeira subida do volume do Postgres, infra/sql/initdb.sh roda infra/sql/papeis.sql e define as senhas de APP_OWNER_SENHA e APP_USER_SENHA. Depois, make migrate aplica as migrações como app_owner.

Trocar uma senha depois da criação do volume exige ALTER ROLE como superusuário, porque o initdb não roda de novo.

## Produção (RDS)

1. Conectar com a conta administrativa do RDS, por rede privada e com TLS.
2. Rodar infra/sql/papeis.sql no banco da aplicação. O script é idempotente.
3. Definir as senhas com o psql, sem deixar o valor no histórico do shell:

   ```
   \password app_owner
   \password app_user
   ```

4. Guardar as senhas no gerenciador de segredos; a aplicação recebe só DATABASE_URL (app_user) e o job de migração recebe só MIGRACAO_DATABASE_URL (app_owner).
5. Rodar as migrações com o job de migração e conferir com make test (suíte tests/isolation) num ambiente de homologação.

## Conferência rápida

```sql
SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
FROM pg_roles WHERE rolname LIKE 'app_%';
```

Nenhum dos três pode ter rolsuper ou rolbypassrls; app_verificador não tem login.
