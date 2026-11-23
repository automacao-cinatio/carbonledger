"""Integridade: verificacoes periodicas da trilha, dos calculos, dos fechamentos e dos arquivos.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-23

- integridade_verificacao: resultado de cada verificacao de um tenant (agendada ou pedida na
  tela), com as contagens e a lista de falhas. So insercao. RLS de tenant; o verificador nao ve.
- identidade_tenants(): unica forma de o disparo diario (tarefa global do beat) saber quais
  tenants verificar, antes de haver contexto. Revisao de seguranca: funcao do app_identidade,
  so leitura, devolve so os ids dos tenants (nenhum dado de cliente); o disparo so enfileira
  uma tarefa por tenant, com tenant_id explicito.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINIDOR = "SECURITY DEFINER SET search_path = pg_catalog, public"

COMANDOS = [
    """
    CREATE TABLE integridade_verificacao (
        tenant_id uuid NOT NULL,
        verificacao_id uuid NOT NULL,
        origem text NOT NULL,
        iniciada_em timestamptz NOT NULL,
        concluida_em timestamptz NOT NULL DEFAULT clock_timestamp(),
        contagens jsonb NOT NULL,
        falhas jsonb NOT NULL,
        integra boolean NOT NULL,
        pedida_por uuid,
        CONSTRAINT pk_integridade_verificacao PRIMARY KEY (tenant_id, verificacao_id),
        CONSTRAINT fk_integridade_verificacao_tenant FOREIGN KEY (tenant_id)
            REFERENCES tenant (tenant_id),
        CONSTRAINT ck_integridade_verificacao_origem CHECK (origem IN ('agendada', 'manual')),
        CONSTRAINT ck_integridade_verificacao_falhas CHECK (
            jsonb_typeof(falhas) = 'array' AND integra = (jsonb_array_length(falhas) = 0)
        )
    )
    """,
    "CREATE INDEX ix_integridade_verificacao_tenant_id_concluida_em"
    " ON integridade_verificacao (tenant_id, concluida_em)",
    f"""
    CREATE FUNCTION identidade_tenants() RETURNS SETOF uuid
    LANGUAGE sql STABLE {DEFINIDOR} AS $$
        SELECT tenant_id FROM tenant ORDER BY tenant_id
    $$
    """,
    "REVOKE ALL ON FUNCTION identidade_tenants() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identidade_tenants() TO app_user",
    "ALTER FUNCTION identidade_tenants() OWNER TO app_identidade",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for comando in sql_rls_tabela("integridade_verificacao", por_empresa=False):
        op.execute(comando)
    for comando in sql_conceder("integridade_verificacao", ["SELECT", "INSERT"]):
        op.execute(comando)
    op.execute(
        "CREATE POLICY integridade_verificacao_verificador ON integridade_verificacao"
        " AS RESTRICTIVE FOR SELECT TO app_verificador USING (false)"
    )
    op.execute(
        "CREATE TRIGGER integridade_verificacao_sem_alteracao BEFORE UPDATE OR DELETE ON"
        " integridade_verificacao FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
    )
    op.execute(
        "CREATE TRIGGER integridade_verificacao_sem_truncate BEFORE TRUNCATE ON"
        " integridade_verificacao FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL ROLE app_identidade")
    op.execute("DROP FUNCTION identidade_tenants()")
    op.execute("RESET ROLE")
    op.execute("DROP TABLE integridade_verificacao")
