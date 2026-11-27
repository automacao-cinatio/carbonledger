"""Pedido de verificacao de integridade: o "Verificar agora" sai da requisicao e vai para o worker.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-27

A verificacao refaz todos os calculos, rele todos os arquivos do S3 e confere a trilha
inteira: cresce sem parar com o uso (11 s com 12 empresas e dois anos no teste de volume) e
passaria do tempo do balanceador. Como o relatorio (0020):

- integridade_pedido: um por clique. A tela grava o pedido; o worker verifica e marca
  concluido (com a verificacao gravada) ou falhou (com a mensagem que diz o que fazer).
- So um pedido na fila por tenant (indice unico parcial): clique duplo nao verifica duas vezes.
- O app_user so muda o desfecho (situacao, verificacao, mensagem e hora); quem pediu e quando
  nao mudam depois de gravados.
O verificador nao ve pedidos, como nao ve as verificacoes.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMANDOS = [
    """
    CREATE TABLE integridade_pedido (
        tenant_id uuid NOT NULL,
        pedido_id uuid NOT NULL,
        pedido_por uuid NOT NULL,
        pedido_em timestamptz NOT NULL DEFAULT now(),
        situacao text NOT NULL DEFAULT 'na_fila',
        verificacao_id uuid,
        mensagem text,
        concluido_em timestamptz,
        CONSTRAINT pk_integridade_pedido PRIMARY KEY (tenant_id, pedido_id),
        CONSTRAINT fk_integridade_pedido_verificacao
            FOREIGN KEY (tenant_id, verificacao_id)
            REFERENCES integridade_verificacao (tenant_id, verificacao_id),
        CONSTRAINT ck_integridade_pedido_situacao
            CHECK (situacao IN ('na_fila', 'concluido', 'falhou')),
        CONSTRAINT ck_integridade_pedido_desfecho CHECK (
            (situacao = 'concluido') = (verificacao_id IS NOT NULL)
            AND (situacao = 'falhou') = (mensagem IS NOT NULL)
            AND (situacao = 'na_fila') = (concluido_em IS NULL)
        )
    )
    """,
    "CREATE UNIQUE INDEX uq_integridade_pedido_tenant_id_na_fila"
    " ON integridade_pedido (tenant_id) WHERE situacao = 'na_fila'",
    "CREATE INDEX ix_integridade_pedido_tenant_id_pedido_em"
    " ON integridade_pedido (tenant_id, pedido_em)",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for comando in sql_rls_tabela("integridade_pedido", por_empresa=False):
        op.execute(comando)
    for comando in sql_conceder("integridade_pedido", ["SELECT", "INSERT"]):
        op.execute(comando)
    op.execute(
        "CREATE POLICY integridade_pedido_verificador ON integridade_pedido AS RESTRICTIVE"
        " FOR SELECT TO app_verificador USING (false)"
    )
    op.execute(
        "GRANT UPDATE (situacao, verificacao_id, mensagem, concluido_em)"
        " ON integridade_pedido TO app_user"
    )


def downgrade() -> None:
    op.execute("DROP TABLE integridade_pedido")
