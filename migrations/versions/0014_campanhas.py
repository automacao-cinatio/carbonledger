"""Campanhas de coleta: prazo, aviso a equipe e lembretes.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-23

- campanha: uma por inventario aberta de cada vez (indice unico parcial), com prazo e mensagem.
  Encerrar e a unica mudanca (encerrada_em); nada se apaga.
- campanha_envio: cada aviso enviado (abertura, lembretes de 7, 3 e 1 dia, vencida), uma vez
  por tipo (chave unica), com quantos destinatarios. So insercao: e o que impede repetir aviso.
O verificador nao ve nada daqui.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMANDOS = [
    """
    CREATE TABLE campanha (
        tenant_id uuid NOT NULL,
        campanha_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        prazo date NOT NULL,
        mensagem text,
        criada_por uuid,
        criada_em timestamptz NOT NULL DEFAULT now(),
        encerrada_em timestamptz,
        motivo_encerramento text,
        CONSTRAINT pk_campanha PRIMARY KEY (tenant_id, campanha_id),
        CONSTRAINT uq_campanha_tenant_id_campanha_id_empresa_id
            UNIQUE (tenant_id, campanha_id, empresa_id),
        CONSTRAINT fk_campanha_inventario FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT ck_campanha_mensagem CHECK (mensagem IS NULL OR length(mensagem) <= 1000),
        CONSTRAINT ck_campanha_encerramento CHECK (
            (encerrada_em IS NULL) = (motivo_encerramento IS NULL)
        ),
        CONSTRAINT ck_campanha_motivo CHECK (
            motivo_encerramento IS NULL
            OR motivo_encerramento IN ('manual', 'coleta_completa', 'inventario_fechado')
        )
    )
    """,
    "CREATE UNIQUE INDEX uq_campanha_tenant_id_inventario_id_aberta ON campanha"
    " (tenant_id, inventario_id) WHERE encerrada_em IS NULL",
    """
    CREATE TABLE campanha_envio (
        tenant_id uuid NOT NULL,
        campanha_id uuid NOT NULL,
        tipo text NOT NULL,
        empresa_id uuid NOT NULL,
        destinatarios integer NOT NULL,
        enviado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_campanha_envio PRIMARY KEY (tenant_id, campanha_id, tipo),
        CONSTRAINT fk_campanha_envio_campanha FOREIGN KEY (tenant_id, campanha_id, empresa_id)
            REFERENCES campanha (tenant_id, campanha_id, empresa_id),
        CONSTRAINT ck_campanha_envio_tipo CHECK (
            tipo IN ('abertura', 'lembrete_7', 'lembrete_3', 'lembrete_1', 'vencida')
        ),
        CONSTRAINT ck_campanha_envio_destinatarios CHECK (destinatarios >= 0)
    )
    """,
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela, privilegios in (
        ("campanha", ["SELECT", "INSERT", "UPDATE"]),
        ("campanha_envio", ["SELECT", "INSERT"]),
    ):
        for comando in sql_rls_tabela(tabela, por_empresa=True):
            op.execute(comando)
        for comando in sql_conceder(tabela, privilegios):
            op.execute(comando)
        op.execute(
            f"CREATE POLICY {tabela}_verificador ON {tabela} AS RESTRICTIVE FOR SELECT"
            " TO app_verificador USING (false)"
        )
    op.execute(
        "CREATE TRIGGER campanha_envio_sem_alteracao BEFORE UPDATE OR DELETE ON campanha_envio"
        " FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
    )
    op.execute(
        "CREATE TRIGGER campanha_envio_sem_truncate BEFORE TRUNCATE ON campanha_envio"
        " FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE campanha_envio")
    op.execute("DROP TABLE campanha")
