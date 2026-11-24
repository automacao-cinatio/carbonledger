"""Escopo 2: total separado no calculo.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-23

O metodo fator_direto 1.1.0 calcula os escopos 1 e 2 juntos, com o fator do SIN do mes do
consumo para a energia eletrica. As execucoes 1.0.0 ja gravadas ficam com escopo 2 igual a
zero, o que e verdade: aquela versao nao calculava o escopo 2.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Coluna nova com DEFAULT: o Postgres nao reescreve as linhas, entao a trigger de so
    # insercao nao dispara.
    op.execute("ALTER TABLE calculo ADD COLUMN escopo2_tco2e numeric NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE calculo DROP COLUMN escopo2_tco2e")
