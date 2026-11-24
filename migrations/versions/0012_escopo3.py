"""Escopo 3: total separado no calculo.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-23

O metodo fator_direto 1.2.0 calcula tambem as 15 categorias do escopo 3. Execucoes anteriores
ficam com escopo 3 igual a zero, o que e verdade: aquelas versoes nao o calculavam.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Coluna nova com DEFAULT: o Postgres nao reescreve as linhas, e a trigger de so insercao
    # nao dispara.
    op.execute("ALTER TABLE calculo ADD COLUMN escopo3_tco2e numeric NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE calculo DROP COLUMN escopo3_tco2e")
