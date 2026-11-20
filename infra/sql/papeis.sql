-- Papeis do banco do CarbonLedger. Idempotente: pode rodar de novo sem efeito colateral.
-- Roda como superusuario (initdb em dev; conta administrativa do RDS em producao).
-- As senhas nao ficam aqui: initdb.sh (ou o runbook de producao) define cada uma.
--
-- app_owner        dono das tabelas; so as migracoes conectam com ele.
-- app_user         API e workers; sem BYPASSRLS e sem ser dono de nada.
-- app_verificador  sem login; o app_user entra nele com SET LOCAL ROLE e so le.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_owner') THEN
        CREATE ROLE app_owner LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_verificador') THEN
        CREATE ROLE app_verificador NOLOGIN;
    END IF;
END
$$;

-- Atributos reafirmados a cada execucao: nenhum papel da aplicacao contorna RLS.
ALTER ROLE app_owner NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN;
ALTER ROLE app_user NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN;
ALTER ROLE app_verificador NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOLOGIN;

-- O app_user pode assumir o app_verificador (SET LOCAL ROLE), mas nao herda nada dele.
GRANT app_verificador TO app_user WITH INHERIT FALSE, SET TRUE;

-- Banco e esquema: so os papeis da aplicacao conectam; so o app_owner cria objetos.
DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO app_owner', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_user', current_database());
END
$$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO app_owner;
GRANT USAGE ON SCHEMA public TO app_user, app_verificador;
