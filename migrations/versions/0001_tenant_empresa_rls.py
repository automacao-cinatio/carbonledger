"""Funcoes de contexto, tabelas tenant e empresa, RLS com falha fechada.

Revision ID: 0001
Revises:
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.rls import PAPEL_APP, PAPEL_VERIFICADOR, sql_conceder, sql_rls_tabela

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Valor vazio ou ausente vira NULL; NULL nunca casa com tenant_id, entao nada aparece.
FUNCOES_CONTEXTO = {
    "contexto_tenant_id()": (
        "uuid",
        "SELECT nullif(current_setting('app.tenant_id', true), '')::uuid",
    ),
    "contexto_escopo()": (
        "text",
        "SELECT nullif(current_setting('app.escopo', true), '')",
    ),
    "contexto_empresas()": (
        "uuid[]",
        "SELECT coalesce(nullif(current_setting('app.empresas', true), '')::uuid[], '{}')",
    ),
    "contexto_usuario_id()": (
        "uuid",
        "SELECT nullif(current_setting('app.usuario_id', true), '')::uuid",
    ),
}


def upgrade() -> None:
    for assinatura, (retorno, corpo) in FUNCOES_CONTEXTO.items():
        op.execute(
            f"CREATE FUNCTION {assinatura} RETURNS {retorno}"
            f" LANGUAGE sql STABLE PARALLEL SAFE AS $${corpo}$$"
        )
        op.execute(f"REVOKE ALL ON FUNCTION {assinatura} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {assinatura} TO {PAPEL_APP}, {PAPEL_VERIFICADOR}")

    op.create_table(
        "tenant",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant"),
    )
    op.create_table(
        "empresa",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("empresa_id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "empresa_id", name="pk_empresa"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.tenant_id"], name="fk_empresa_tenant_id_tenant"
        ),
    )

    # Nada some: sem DELETE para o app_user nestas tabelas.
    for tabela, por_empresa in (("tenant", False), ("empresa", True)):
        for comando in sql_rls_tabela(tabela, por_empresa=por_empresa):
            op.execute(comando)
        for comando in sql_conceder(tabela, ["SELECT", "INSERT", "UPDATE"]):
            op.execute(comando)


def downgrade() -> None:
    op.drop_table("empresa")
    op.drop_table("tenant")
    for assinatura in FUNCOES_CONTEXTO:
        op.execute(f"DROP FUNCTION {assinatura}")
