"""Metas de reducao e acoes do plano de descarbonizacao.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-23

- meta: reducao percentual das emissoes de um ou dois escopos, de um ano-base a um ano-alvo.
  O ano-base e um inventario fechado; o valor-base e copiado do calculo daquele fechamento
  (calculo_base_id guarda a origem). Nenhum percentual vem do sistema: e sempre do cliente.
  A meta nao se apaga: arquiva.
- acao_reducao: iniciativa do plano, com a reducao estimada informada pelo cliente.
  E estimativa, nao calculo: nao entra no inventario.
O verificador nao ve nada daqui (ve so o inventario da concessao).
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMANDOS = [
    """
    CREATE TABLE meta (
        tenant_id uuid NOT NULL,
        meta_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        nome text NOT NULL,
        escopos text NOT NULL,
        inventario_base_id uuid NOT NULL,
        calculo_base_id uuid NOT NULL,
        ano_base smallint NOT NULL,
        valor_base_tco2e numeric NOT NULL,
        ano_alvo smallint NOT NULL,
        reducao_percentual numeric NOT NULL,
        ativa boolean NOT NULL DEFAULT true,
        criado_por uuid,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_meta PRIMARY KEY (tenant_id, meta_id),
        CONSTRAINT uq_meta_tenant_id_meta_id_empresa_id UNIQUE (tenant_id, meta_id, empresa_id),
        CONSTRAINT fk_meta_inventario FOREIGN KEY (tenant_id, inventario_base_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT fk_meta_calculo FOREIGN KEY (tenant_id, calculo_base_id, empresa_id)
            REFERENCES calculo (tenant_id, calculo_id, empresa_id),
        CONSTRAINT ck_meta_nome CHECK (length(btrim(nome)) BETWEEN 1 AND 120),
        CONSTRAINT ck_meta_escopos CHECK (escopos IN ('1', '2', '1+2')),
        CONSTRAINT ck_meta_anos CHECK (ano_alvo > ano_base AND ano_alvo <= 2100),
        CONSTRAINT ck_meta_reducao CHECK (reducao_percentual > 0 AND reducao_percentual <= 100),
        CONSTRAINT ck_meta_base CHECK (valor_base_tco2e >= 0)
    )
    """,
    """
    CREATE TABLE acao_reducao (
        tenant_id uuid NOT NULL,
        acao_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        meta_id uuid NOT NULL,
        titulo text NOT NULL,
        descricao text,
        reducao_estimada_tco2e numeric NOT NULL,
        ano_inicio smallint NOT NULL,
        situacao text NOT NULL DEFAULT 'planejada',
        criado_por uuid,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_acao_reducao PRIMARY KEY (tenant_id, acao_id),
        CONSTRAINT fk_acao_reducao_meta FOREIGN KEY (tenant_id, meta_id, empresa_id)
            REFERENCES meta (tenant_id, meta_id, empresa_id),
        CONSTRAINT ck_acao_reducao_titulo CHECK (length(btrim(titulo)) BETWEEN 1 AND 120),
        CONSTRAINT ck_acao_reducao_descricao CHECK (descricao IS NULL OR length(descricao) <= 500),
        CONSTRAINT ck_acao_reducao_estimativa CHECK (reducao_estimada_tco2e >= 0),
        CONSTRAINT ck_acao_reducao_ano CHECK (ano_inicio BETWEEN 2000 AND 2100),
        CONSTRAINT ck_acao_reducao_situacao CHECK (
            situacao IN ('planejada', 'em_andamento', 'concluida', 'cancelada')
        )
    )
    """,
    "CREATE INDEX ix_acao_reducao_tenant_id_meta_id ON acao_reducao (tenant_id, meta_id)",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela in ("meta", "acao_reducao"):
        for comando in sql_rls_tabela(tabela, por_empresa=True):
            op.execute(comando)
        for comando in sql_conceder(tabela, ["SELECT", "INSERT", "UPDATE"]):
            op.execute(comando)
        op.execute(
            f"CREATE POLICY {tabela}_verificador ON {tabela} AS RESTRICTIVE FOR SELECT"
            " TO app_verificador USING (false)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE acao_reducao")
    op.execute("DROP TABLE meta")
