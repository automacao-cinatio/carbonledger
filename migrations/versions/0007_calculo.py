"""Calculo do escopo 1: atividade da fonte, execucoes, memoria de calculo e pendencias.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-23

- fonte_emissao.atividade: codigo do combustivel ou da atividade (o mesmo dos fatores e das
  misturas em data/). Sem atividade, a fonte vira pendencia no calculo.
- fator_versao: a chave unica passa a incluir a categoria. O mesmo componente (diesel fossil)
  tem fatores diferentes em combustao movel e estacionaria.
- calculo: uma execucao do metodo sobre um inventario, com o instante de conhecimento dos
  fatores, a entrada canonica (jsonb), o hash da entrada e o hash do resultado. Refazer a
  partir da entrada guardada tem de dar o mesmo hash do resultado.
- calculo_linha: memoria de calculo, uma linha por lancamento, componente da mistura e gas.
- calculo_pendencia: o que impediu de calcular (nunca vira zero).
As tres tabelas sao so de insercao: calcular de novo cria execucao nova.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CODIGO = r"'^[a-z0-9_]+(\.[a-z0-9_]+)*$'"
TABELAS = ("calculo", "calculo_linha", "calculo_pendencia")

COMANDOS = [
    f"ALTER TABLE fonte_emissao ADD COLUMN atividade text"
    f" CONSTRAINT ck_fonte_emissao_atividade CHECK (atividade IS NULL OR atividade ~ {CODIGO})",
    "ALTER TABLE fator_versao DROP CONSTRAINT uq_fator_versao_chave",
    "ALTER TABLE fator_versao ADD CONSTRAINT uq_fator_versao_chave"
    " UNIQUE (lote_id, categoria, codigo, gas, biogenico, vigencia_inicio)",
    "CREATE INDEX ix_fator_versao_categoria_codigo ON fator_versao (categoria, codigo)",
    """
    CREATE TABLE calculo (
        tenant_id uuid NOT NULL,
        calculo_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        metodo text NOT NULL,
        metodo_versao text NOT NULL,
        gwp_conjunto text NOT NULL,
        conhecido_em timestamptz NOT NULL,
        entrada jsonb NOT NULL,
        hash_entrada bytea NOT NULL,
        hash_resultado bytea NOT NULL,
        escopo1_tco2e numeric NOT NULL,
        biogenico_tco2 numeric NOT NULL,
        linhas integer NOT NULL,
        pendencias integer NOT NULL,
        calculado_por uuid,
        calculado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_calculo PRIMARY KEY (tenant_id, calculo_id),
        CONSTRAINT uq_calculo_tenant_id_calculo_id_empresa_id
            UNIQUE (tenant_id, calculo_id, empresa_id),
        CONSTRAINT fk_calculo_inventario FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT ck_calculo_metodo_versao CHECK (metodo_versao ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$'),
        CONSTRAINT ck_calculo_hashes CHECK (length(hash_entrada) = 32 AND length(hash_resultado) = 32),
        CONSTRAINT ck_calculo_contagens CHECK (linhas >= 0 AND pendencias >= 0)
    )
    """,
    "CREATE INDEX ix_calculo_tenant_id_inventario_id ON calculo"
    " (tenant_id, inventario_id, calculado_em)",
    """
    CREATE TABLE calculo_linha (
        tenant_id uuid NOT NULL,
        calculo_id uuid NOT NULL,
        linha integer NOT NULL,
        empresa_id uuid NOT NULL,
        fonte_id uuid NOT NULL,
        lancamento_id uuid NOT NULL,
        periodo smallint NOT NULL,
        categoria text NOT NULL,
        atividade text NOT NULL,
        componente text NOT NULL,
        percentual_mistura numeric,
        mistura_versao_id uuid,
        data_referencia date NOT NULL,
        quantidade numeric NOT NULL,
        unidade text NOT NULL,
        quantidade_convertida numeric NOT NULL,
        gas text NOT NULL,
        biogenico boolean NOT NULL,
        fator numeric NOT NULL,
        unidade_fator text NOT NULL,
        fator_versao_id uuid NOT NULL,
        gwp numeric,
        gwp_versao_id uuid,
        percentual_consolidado numeric NOT NULL,
        massa_t numeric NOT NULL,
        tco2e numeric,
        CONSTRAINT pk_calculo_linha PRIMARY KEY (tenant_id, calculo_id, linha),
        CONSTRAINT fk_calculo_linha_calculo FOREIGN KEY (tenant_id, calculo_id, empresa_id)
            REFERENCES calculo (tenant_id, calculo_id, empresa_id),
        CONSTRAINT ck_calculo_linha_biogenico CHECK (
            (biogenico AND tco2e IS NULL AND gwp IS NULL)
            OR (NOT biogenico AND tco2e IS NOT NULL AND gwp IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE calculo_pendencia (
        tenant_id uuid NOT NULL,
        calculo_id uuid NOT NULL,
        item integer NOT NULL,
        empresa_id uuid NOT NULL,
        fonte_id uuid NOT NULL,
        periodo smallint,
        motivo text NOT NULL,
        detalhe text NOT NULL,
        CONSTRAINT pk_calculo_pendencia PRIMARY KEY (tenant_id, calculo_id, item),
        CONSTRAINT fk_calculo_pendencia_calculo FOREIGN KEY (tenant_id, calculo_id, empresa_id)
            REFERENCES calculo (tenant_id, calculo_id, empresa_id),
        CONSTRAINT ck_calculo_pendencia_motivo CHECK (motivo IN (
            'falta_dado', 'sem_atividade', 'sem_fator', 'unidade_incompativel', 'sem_gwp'
        ))
    )
    """,
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela in TABELAS:
        for comando in sql_rls_tabela(tabela, por_empresa=True):
            op.execute(comando)
        for comando in sql_conceder(tabela, ["SELECT", "INSERT"]):
            op.execute(comando)
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_alteracao BEFORE UPDATE OR DELETE ON {tabela}"
            " FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
        )
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_truncate BEFORE TRUNCATE ON {tabela}"
            " FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
        )


def downgrade() -> None:
    for tabela in reversed(TABELAS):
        op.execute(f"DROP TABLE {tabela}")
    op.execute("DROP INDEX ix_fator_versao_categoria_codigo")
    op.execute("ALTER TABLE fator_versao DROP CONSTRAINT uq_fator_versao_chave")
    op.execute(
        "ALTER TABLE fator_versao ADD CONSTRAINT uq_fator_versao_chave"
        " UNIQUE (lote_id, codigo, gas, biogenico, vigencia_inicio)"
    )
    op.execute("ALTER TABLE fonte_emissao DROP COLUMN atividade")
