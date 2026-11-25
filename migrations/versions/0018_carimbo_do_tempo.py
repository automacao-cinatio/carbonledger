"""Carimbo do tempo no relatorio assinado (PAdES B-T).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-24

- relatorio_inventario.carimbo_em: a hora atestada pela autoridade de carimbo do tempo (TSA)
  no token embutido na assinatura; NULL quando o relatorio foi assinado sem TSA (PAdES B-B).
  Gravada junto com o resto, na insercao (a tabela continua so de insercao).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE relatorio_inventario ADD COLUMN carimbo_em timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE relatorio_inventario DROP COLUMN carimbo_em")
