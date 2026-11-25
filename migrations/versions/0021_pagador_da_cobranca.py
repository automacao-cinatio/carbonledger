"""Pagador da cobranca: dados proprios de cobranca do cliente (para o Asaas).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-25

- assinatura ganha razao social, CNPJ (numerico ou alfanumerico) e e-mail de cobranca do
  pagador, preenchidos pelo administrador na tela Cobranca. Os tres andam juntos.
- pagador_sincronizado: falso quando o pagador muda; a rodada diaria atualiza o cliente no
  gateway antes da proxima fatura e marca verdadeiro.
O app_user ja tinha UPDATE em assinatura (migracao 0019); o verificador continua sem ver nada.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE assinatura
            ADD COLUMN pagador_nome text,
            ADD COLUMN pagador_cnpj text,
            ADD COLUMN pagador_email text,
            ADD COLUMN pagador_sincronizado boolean NOT NULL DEFAULT false,
            ADD CONSTRAINT ck_assinatura_pagador_completo CHECK (
                (pagador_nome IS NULL) = (pagador_cnpj IS NULL)
                AND (pagador_nome IS NULL) = (pagador_email IS NULL)
            ),
            ADD CONSTRAINT ck_assinatura_pagador_cnpj
                CHECK (pagador_cnpj ~ '^[0-9A-Z]{12}[0-9]{2}$')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE assinatura
            DROP CONSTRAINT ck_assinatura_pagador_cnpj,
            DROP CONSTRAINT ck_assinatura_pagador_completo,
            DROP COLUMN pagador_sincronizado,
            DROP COLUMN pagador_email,
            DROP COLUMN pagador_cnpj,
            DROP COLUMN pagador_nome
        """
    )
