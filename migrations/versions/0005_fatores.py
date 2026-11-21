"""Fatores de emissao, GWP e misturas: referencia global, bitemporal e so de insercao.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

Tabelas globais (nao sao dado de cliente). Cada importacao de arquivo vira um lote; cada
linha vira uma versao. O lote so vale depois da decisao (aprovado), registrada uma unica vez.

Bitemporal:
- tempo de validade: vigencia_inicio e vigencia_fim de cada versao (fatores e misturas);
- tempo de conhecimento: decidido_em da aprovacao do lote.
Para saber o fator de uma data "como era conhecido" num instante, filtra-se a vigencia pela
data e a aprovacao pelo instante; entre as versoes que sobram, vale a aprovada por ultimo.

Nunca UPDATE nem DELETE (trigger somente_insercao): corrigir um valor e importar versao nova.
So o papel app_curadoria insere; o app_user (API e workers) so le.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAPEL_APP = "app_user"
PAPEL_CURADORIA = "app_curadoria"
TABELAS = ("lote_importacao", "lote_decisao", "gwp_versao", "fator_versao", "mistura_versao")
CODIGO = r"'^[a-z0-9_]+(\.[a-z0-9_]+)*$'"
GAS = r"'^[A-Z0-9][A-Za-z0-9-]*$'"

COMANDOS = [
    """
    CREATE TABLE lote_importacao (
        lote_id uuid NOT NULL,
        tipo text NOT NULL,
        arquivo text NOT NULL,
        sha256 text NOT NULL,
        linhas integer NOT NULL,
        importado_por text NOT NULL,
        importado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_lote_importacao PRIMARY KEY (lote_id),
        CONSTRAINT uq_lote_importacao_tipo_sha256 UNIQUE (tipo, sha256),
        CONSTRAINT ck_lote_importacao_tipo CHECK (tipo IN ('gwp', 'fatores', 'misturas')),
        CONSTRAINT ck_lote_importacao_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_lote_importacao_linhas CHECK (linhas >= 0),
        CONSTRAINT ck_lote_importacao_importado_por CHECK (length(btrim(importado_por)) > 0)
    )
    """,
    """
    CREATE TABLE lote_decisao (
        lote_id uuid NOT NULL,
        decisao text NOT NULL,
        responsavel text NOT NULL,
        justificativa text,
        decidido_em timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_lote_decisao PRIMARY KEY (lote_id),
        CONSTRAINT fk_lote_decisao_lote_id_lote_importacao FOREIGN KEY (lote_id)
            REFERENCES lote_importacao (lote_id),
        CONSTRAINT ck_lote_decisao_decisao CHECK (decisao IN ('aprovado', 'rejeitado')),
        CONSTRAINT ck_lote_decisao_responsavel CHECK (length(btrim(responsavel)) > 0),
        CONSTRAINT ck_lote_decisao_rejeicao_justificada CHECK (
            decisao = 'aprovado' OR length(btrim(coalesce(justificativa, ''))) > 0
        )
    )
    """,
    f"""
    CREATE TABLE gwp_versao (
        gwp_versao_id uuid NOT NULL,
        lote_id uuid NOT NULL,
        conjunto text NOT NULL,
        gas text NOT NULL,
        valor numeric NOT NULL,
        fonte text NOT NULL,
        hash_conteudo text NOT NULL,
        CONSTRAINT pk_gwp_versao PRIMARY KEY (gwp_versao_id),
        CONSTRAINT fk_gwp_versao_lote_id_lote_importacao FOREIGN KEY (lote_id)
            REFERENCES lote_importacao (lote_id),
        CONSTRAINT uq_gwp_versao_lote_id_conjunto_gas UNIQUE (lote_id, conjunto, gas),
        CONSTRAINT ck_gwp_versao_conjunto CHECK (conjunto ~ '^[A-Z0-9_]+$'),
        CONSTRAINT ck_gwp_versao_gas CHECK (gas ~ {GAS}),
        CONSTRAINT ck_gwp_versao_valor CHECK (valor > 0),
        CONSTRAINT ck_gwp_versao_fonte CHECK (length(btrim(fonte)) > 0)
    )
    """,
    "CREATE INDEX ix_gwp_versao_conjunto_gas ON gwp_versao (conjunto, gas)",
    f"""
    CREATE TABLE fator_versao (
        fator_versao_id uuid NOT NULL,
        lote_id uuid NOT NULL,
        codigo text NOT NULL,
        descricao text NOT NULL,
        categoria text NOT NULL,
        gas text NOT NULL,
        biogenico boolean NOT NULL,
        valor numeric NOT NULL,
        unidade text NOT NULL,
        vigencia_inicio date NOT NULL,
        vigencia_fim date,
        fonte text NOT NULL,
        referencia text,
        hash_conteudo text NOT NULL,
        CONSTRAINT pk_fator_versao PRIMARY KEY (fator_versao_id),
        CONSTRAINT fk_fator_versao_lote_id_lote_importacao FOREIGN KEY (lote_id)
            REFERENCES lote_importacao (lote_id),
        CONSTRAINT uq_fator_versao_chave UNIQUE (lote_id, codigo, gas, biogenico, vigencia_inicio),
        CONSTRAINT ck_fator_versao_codigo CHECK (codigo ~ {CODIGO}),
        CONSTRAINT ck_fator_versao_categoria CHECK (categoria ~ {CODIGO}),
        CONSTRAINT ck_fator_versao_gas CHECK (gas ~ {GAS}),
        CONSTRAINT ck_fator_versao_valor CHECK (valor >= 0),
        CONSTRAINT ck_fator_versao_vigencia CHECK (vigencia_fim IS NULL OR vigencia_fim >= vigencia_inicio),
        CONSTRAINT ck_fator_versao_fonte CHECK (length(btrim(fonte)) > 0)
    )
    """,
    "CREATE INDEX ix_fator_versao_busca ON fator_versao (codigo, gas, biogenico, vigencia_inicio)",
    f"""
    CREATE TABLE mistura_versao (
        mistura_versao_id uuid NOT NULL,
        lote_id uuid NOT NULL,
        combustivel text NOT NULL,
        componente text NOT NULL,
        percentual numeric NOT NULL,
        vigencia_inicio date NOT NULL,
        vigencia_fim date,
        fonte text NOT NULL,
        hash_conteudo text NOT NULL,
        CONSTRAINT pk_mistura_versao PRIMARY KEY (mistura_versao_id),
        CONSTRAINT fk_mistura_versao_lote_id_lote_importacao FOREIGN KEY (lote_id)
            REFERENCES lote_importacao (lote_id),
        CONSTRAINT uq_mistura_versao_chave
            UNIQUE (lote_id, combustivel, componente, vigencia_inicio),
        CONSTRAINT ck_mistura_versao_combustivel CHECK (combustivel ~ {CODIGO}),
        CONSTRAINT ck_mistura_versao_componente CHECK (componente ~ {CODIGO}),
        CONSTRAINT ck_mistura_versao_percentual CHECK (percentual >= 0 AND percentual <= 100),
        CONSTRAINT ck_mistura_versao_vigencia CHECK (vigencia_fim IS NULL OR vigencia_fim >= vigencia_inicio),
        CONSTRAINT ck_mistura_versao_fonte CHECK (length(btrim(fonte)) > 0)
    )
    """,
    "CREATE INDEX ix_mistura_versao_busca ON mistura_versao (combustivel, vigencia_inicio)",
    """
    CREATE FUNCTION somente_insercao() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    BEGIN
        RAISE EXCEPTION '% so aceita insercao: corrija com uma versao nova', TG_TABLE_NAME
            USING ERRCODE = 'CL003';
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION somente_insercao() FROM PUBLIC",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela in TABELAS:
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_alteracao BEFORE UPDATE OR DELETE ON {tabela}"
            " FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
        )
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_truncate BEFORE TRUNCATE ON {tabela}"
            " FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
        )
        op.execute(f"REVOKE ALL ON {tabela} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON {tabela} TO {PAPEL_APP}")
        op.execute(f"GRANT SELECT, INSERT ON {tabela} TO {PAPEL_CURADORIA}")


def downgrade() -> None:
    # Apagar a referencia de fatores so faz sentido em banco de desenvolvimento.
    for tabela in reversed(TABELAS):
        op.execute(f"DROP TABLE {tabela}")
    op.execute("DROP FUNCTION somente_insercao()")
