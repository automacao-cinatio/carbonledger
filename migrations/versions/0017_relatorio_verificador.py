"""O verificador ve o relatorio assinado do inventario que recebeu (continuacao do P19).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-24

- relatorio_inventario: o verificador ve os relatorios do inventario da concessao valida
  (mesma regra das outras tabelas por inventario, migracao 0010).
- certificado_assinatura: o verificador ve so os certificados que assinaram esses relatorios
  e so as colunas publicas: a coluna chave_cifrada sai do SELECT dele (privilegio por coluna).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUNAS_PUBLICAS = (
    "tenant_id, certificado_id, empresa_id, titular, cnpj, emissor, numero_serie, valido_de,"
    " valido_ate, impressao_sha256, enviado_por, enviado_em, desativado_em, desativado_por"
)


def upgrade() -> None:
    op.execute("DROP POLICY relatorio_inventario_verificador ON relatorio_inventario")
    op.execute(
        "CREATE POLICY relatorio_inventario_verificador ON relatorio_inventario AS RESTRICTIVE"
        " FOR SELECT TO app_verificador"
        " USING (inventario_id = (SELECT verificacao_inventario()))"
    )
    op.execute("DROP POLICY certificado_assinatura_verificador ON certificado_assinatura")
    op.execute(
        "CREATE POLICY certificado_assinatura_verificador ON certificado_assinatura"
        " AS RESTRICTIVE FOR SELECT TO app_verificador USING (certificado_id IN"
        " (SELECT r.certificado_id FROM relatorio_inventario r))"
    )
    op.execute("REVOKE SELECT ON certificado_assinatura FROM app_verificador")
    op.execute(f"GRANT SELECT ({COLUNAS_PUBLICAS}) ON certificado_assinatura TO app_verificador")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON certificado_assinatura FROM app_verificador")
    op.execute("GRANT SELECT ON certificado_assinatura TO app_verificador")
    for tabela in ("relatorio_inventario", "certificado_assinatura"):
        op.execute(f"DROP POLICY {tabela}_verificador ON {tabela}")
        op.execute(
            f"CREATE POLICY {tabela}_verificador ON {tabela} AS RESTRICTIVE FOR SELECT"
            " TO app_verificador USING (false)"
        )
