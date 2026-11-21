#!/bin/sh
# Roda uma vez, quando o volume do Postgres e criado (docker-entrypoint-initdb.d).
# Cria os papeis da aplicacao e define as senhas vindas das variaveis de ambiente.
# Sem set -u: se o entrypoint fizer source deste arquivo, a opcao vazaria para ele.
set -e

: "${APP_OWNER_SENHA:?defina APP_OWNER_SENHA no .env}"
: "${APP_USER_SENHA:?defina APP_USER_SENHA no .env}"
: "${CURADORIA_SENHA:?defina CURADORIA_SENHA no .env}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -f /infra/sql/papeis.sql

# As senhas passam como variaveis do psql (:'nome'), que as cita com seguranca.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v owner_senha="$APP_OWNER_SENHA" -v user_senha="$APP_USER_SENHA" \
    -v curadoria_senha="$CURADORIA_SENHA" <<'EOSQL'
ALTER ROLE app_owner PASSWORD :'owner_senha';
ALTER ROLE app_user PASSWORD :'user_senha';
ALTER ROLE app_curadoria PASSWORD :'curadoria_senha';
EOSQL
