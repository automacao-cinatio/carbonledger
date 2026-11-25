"""Pedido de relatorio: a geracao do PDF assinado sai da requisicao e vai para o worker.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-25

- relatorio_pedido: um por clique em "Gerar". A tela valida na hora (inventario fechado,
  cobranca em dia, certificado ativo) e grava o pedido na fila; o worker gera, assina, guarda
  e marca pronto (com o relatorio) ou falhou (com a mensagem que diz o que fazer).
- So um pedido na fila por inventario (indice unico parcial): clique duplo nao gera dois.
- O app_user so muda o desfecho (situacao, relatorio, mensagem e hora); quem pediu, quando,
  o inventario e o certificado nao mudam depois de gravados.
O verificador nao ve pedidos: ele recebe o relatorio pronto (relatorio_inventario).
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMANDOS = [
    """
    CREATE TABLE relatorio_pedido (
        tenant_id uuid NOT NULL,
        pedido_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        certificado_id uuid NOT NULL,
        pedido_por uuid NOT NULL,
        pedido_em timestamptz NOT NULL DEFAULT now(),
        situacao text NOT NULL DEFAULT 'na_fila',
        relatorio_id uuid,
        mensagem text,
        concluido_em timestamptz,
        CONSTRAINT pk_relatorio_pedido PRIMARY KEY (tenant_id, pedido_id),
        CONSTRAINT fk_relatorio_pedido_inventario
            FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT fk_relatorio_pedido_certificado
            FOREIGN KEY (tenant_id, certificado_id, empresa_id)
            REFERENCES certificado_assinatura (tenant_id, certificado_id, empresa_id),
        CONSTRAINT fk_relatorio_pedido_relatorio
            FOREIGN KEY (tenant_id, relatorio_id)
            REFERENCES relatorio_inventario (tenant_id, relatorio_id),
        CONSTRAINT ck_relatorio_pedido_situacao
            CHECK (situacao IN ('na_fila', 'pronto', 'falhou')),
        CONSTRAINT ck_relatorio_pedido_desfecho CHECK (
            (situacao = 'pronto') = (relatorio_id IS NOT NULL)
            AND (situacao = 'falhou') = (mensagem IS NOT NULL)
            AND (situacao = 'na_fila') = (concluido_em IS NULL)
        )
    )
    """,
    "CREATE UNIQUE INDEX uq_relatorio_pedido_tenant_id_inventario_id_na_fila"
    " ON relatorio_pedido (tenant_id, inventario_id) WHERE situacao = 'na_fila'",
    "CREATE INDEX ix_relatorio_pedido_tenant_id_inventario_id"
    " ON relatorio_pedido (tenant_id, inventario_id, pedido_em)",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for comando in sql_rls_tabela("relatorio_pedido", por_empresa=True):
        op.execute(comando)
    for comando in sql_conceder("relatorio_pedido", ["SELECT", "INSERT"]):
        op.execute(comando)
    op.execute(
        "CREATE POLICY relatorio_pedido_verificador ON relatorio_pedido AS RESTRICTIVE"
        " FOR SELECT TO app_verificador USING (false)"
    )
    op.execute(
        "GRANT UPDATE (situacao, relatorio_id, mensagem, concluido_em)"
        " ON relatorio_pedido TO app_user"
    )


def downgrade() -> None:
    op.execute("DROP TABLE relatorio_pedido")
