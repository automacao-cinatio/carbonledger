"""Coleta e evidencias: inventario, fontes de emissao, lancamentos, decisoes e evidencias.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23

- inventario: um por empresa e ano, com a abordagem de consolidacao e o conjunto de GWP.
  Nasce em coleta; fechar e reapresentar entram no P11.
- fonte_emissao: o que se coleta em cada unidade (categoria do GHG Protocol, unidade de
  medida, periodicidade) e se a fonte e material. Fonte fora da materialidade exige
  justificativa.
- lancamento: dado de atividade de um periodo da fonte. So insercao: corrigir e gravar uma
  versao nova. Periodo 0 e o ano inteiro (fonte anual); 1 a 12 sao os meses.
- lancamento_decisao: aprovacao ou devolucao de uma versao, gravada uma unica vez. Quem lancou
  nao decide, e so a versao mais recente pode ser decidida (trigger no banco).
- evidencia: arquivo guardado no S3 com Object Lock, com SHA-256. So insercao.
- planilha_importacao: registro de cada planilha de coleta importada. So insercao.

Chaves compostas com empresa_id amarram fonte, lancamento e evidencia a mesma empresa do
inventario e da unidade.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CODIGO = r"'^[a-z0-9_]+(\.[a-z0-9_]+)*$'"
SO_INSERCAO = ("lancamento", "lancamento_decisao", "evidencia", "planilha_importacao")

COMANDOS = [
    # Alvo das chaves compostas: a unidade pertence a uma empresa so.
    "ALTER TABLE unidade ADD CONSTRAINT uq_unidade_tenant_id_unidade_id_empresa_id"
    " UNIQUE (tenant_id, unidade_id, empresa_id)",
    """
    CREATE TABLE inventario (
        tenant_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        ano smallint NOT NULL,
        abordagem text NOT NULL,
        gwp_conjunto text NOT NULL DEFAULT 'AR5_100',
        estado text NOT NULL DEFAULT 'em_coleta',
        criado_por uuid,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_inventario PRIMARY KEY (tenant_id, inventario_id),
        CONSTRAINT uq_inventario_tenant_id_inventario_id_empresa_id
            UNIQUE (tenant_id, inventario_id, empresa_id),
        CONSTRAINT uq_inventario_tenant_id_empresa_id_ano UNIQUE (tenant_id, empresa_id, ano),
        CONSTRAINT fk_inventario_empresa FOREIGN KEY (tenant_id, empresa_id)
            REFERENCES empresa (tenant_id, empresa_id),
        CONSTRAINT ck_inventario_ano CHECK (ano BETWEEN 2000 AND 2100),
        CONSTRAINT ck_inventario_abordagem CHECK (
            abordagem IN ('controle_operacional', 'controle_financeiro', 'participacao_societaria')
        ),
        CONSTRAINT ck_inventario_gwp CHECK (gwp_conjunto ~ '^[A-Z0-9_]+$'),
        CONSTRAINT ck_inventario_estado CHECK (estado IN ('em_coleta'))
    )
    """,
    f"""
    CREATE TABLE fonte_emissao (
        tenant_id uuid NOT NULL,
        fonte_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        unidade_id uuid NOT NULL,
        categoria text NOT NULL,
        descricao text NOT NULL,
        unidade_medida text NOT NULL,
        periodicidade text NOT NULL,
        material boolean NOT NULL DEFAULT true,
        justificativa text,
        ativa boolean NOT NULL DEFAULT true,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_fonte_emissao PRIMARY KEY (tenant_id, fonte_id),
        CONSTRAINT uq_fonte_emissao_tenant_id_fonte_id_empresa_id
            UNIQUE (tenant_id, fonte_id, empresa_id),
        CONSTRAINT fk_fonte_emissao_inventario FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT fk_fonte_emissao_unidade FOREIGN KEY (tenant_id, unidade_id, empresa_id)
            REFERENCES unidade (tenant_id, unidade_id, empresa_id),
        CONSTRAINT ck_fonte_emissao_categoria CHECK (categoria ~ {CODIGO}),
        CONSTRAINT ck_fonte_emissao_descricao CHECK (length(btrim(descricao)) BETWEEN 1 AND 200),
        CONSTRAINT ck_fonte_emissao_unidade_medida CHECK (
            length(btrim(unidade_medida)) BETWEEN 1 AND 40
        ),
        CONSTRAINT ck_fonte_emissao_periodicidade CHECK (periodicidade IN ('mensal', 'anual')),
        CONSTRAINT ck_fonte_emissao_justificativa CHECK (
            material OR length(btrim(coalesce(justificativa, ''))) >= 10
        )
    )
    """,
    "CREATE INDEX ix_fonte_emissao_tenant_id_inventario_id ON fonte_emissao"
    " (tenant_id, inventario_id)",
    """
    CREATE TABLE planilha_importacao (
        tenant_id uuid NOT NULL,
        planilha_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        nome_arquivo text NOT NULL,
        sha256 bytea NOT NULL,
        chave_objeto text NOT NULL,
        linhas_gravadas integer NOT NULL,
        enviado_por uuid,
        enviado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_planilha_importacao PRIMARY KEY (tenant_id, planilha_id),
        CONSTRAINT fk_planilha_importacao_inventario
            FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT ck_planilha_importacao_sha256 CHECK (length(sha256) = 32),
        CONSTRAINT ck_planilha_importacao_linhas CHECK (linhas_gravadas >= 0)
    )
    """,
    """
    CREATE TABLE lancamento (
        tenant_id uuid NOT NULL,
        lancamento_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        fonte_id uuid NOT NULL,
        periodo smallint NOT NULL,
        versao integer NOT NULL,
        quantidade numeric NOT NULL,
        unidade text NOT NULL,
        data_compra date,
        observacao text,
        origem text NOT NULL,
        planilha_id uuid,
        criado_por uuid,
        criado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_lancamento PRIMARY KEY (tenant_id, lancamento_id),
        CONSTRAINT uq_lancamento_tenant_id_fonte_id_periodo_versao
            UNIQUE (tenant_id, fonte_id, periodo, versao),
        CONSTRAINT fk_lancamento_fonte FOREIGN KEY (tenant_id, fonte_id, empresa_id)
            REFERENCES fonte_emissao (tenant_id, fonte_id, empresa_id),
        CONSTRAINT fk_lancamento_planilha FOREIGN KEY (tenant_id, planilha_id)
            REFERENCES planilha_importacao (tenant_id, planilha_id),
        CONSTRAINT ck_lancamento_periodo CHECK (periodo BETWEEN 0 AND 12),
        CONSTRAINT ck_lancamento_versao CHECK (versao >= 1),
        CONSTRAINT ck_lancamento_quantidade CHECK (quantidade >= 0),
        CONSTRAINT ck_lancamento_unidade CHECK (length(btrim(unidade)) BETWEEN 1 AND 40),
        CONSTRAINT ck_lancamento_observacao CHECK (observacao IS NULL OR length(observacao) <= 500),
        CONSTRAINT ck_lancamento_origem CHECK (origem IN ('manual', 'planilha')),
        CONSTRAINT ck_lancamento_planilha CHECK ((origem = 'planilha') = (planilha_id IS NOT NULL))
    )
    """,
    """
    CREATE TABLE lancamento_decisao (
        tenant_id uuid NOT NULL,
        lancamento_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        decisao text NOT NULL,
        motivo text,
        decidido_por uuid NOT NULL,
        decidido_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_lancamento_decisao PRIMARY KEY (tenant_id, lancamento_id),
        CONSTRAINT fk_lancamento_decisao_lancamento FOREIGN KEY (tenant_id, lancamento_id)
            REFERENCES lancamento (tenant_id, lancamento_id),
        CONSTRAINT fk_lancamento_decisao_empresa FOREIGN KEY (tenant_id, empresa_id)
            REFERENCES empresa (tenant_id, empresa_id),
        CONSTRAINT ck_lancamento_decisao_decisao CHECK (decisao IN ('aprovado', 'devolvido')),
        CONSTRAINT ck_lancamento_decisao_motivo CHECK (
            decisao = 'aprovado' OR length(btrim(coalesce(motivo, ''))) >= 5
        ),
        CONSTRAINT ck_lancamento_decisao_motivo_tamanho CHECK (
            motivo IS NULL OR length(motivo) <= 500
        )
    )
    """,
    """
    CREATE TABLE evidencia (
        tenant_id uuid NOT NULL,
        evidencia_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        fonte_id uuid NOT NULL,
        periodo smallint,
        nome_arquivo text NOT NULL,
        tipo_conteudo text NOT NULL,
        tamanho bigint NOT NULL,
        sha256 bytea NOT NULL,
        chave_objeto text NOT NULL,
        versao_objeto text,
        enviado_por uuid,
        enviado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_evidencia PRIMARY KEY (tenant_id, evidencia_id),
        CONSTRAINT fk_evidencia_fonte FOREIGN KEY (tenant_id, fonte_id, empresa_id)
            REFERENCES fonte_emissao (tenant_id, fonte_id, empresa_id),
        CONSTRAINT ck_evidencia_periodo CHECK (periodo IS NULL OR periodo BETWEEN 0 AND 12),
        CONSTRAINT ck_evidencia_nome CHECK (length(btrim(nome_arquivo)) BETWEEN 1 AND 200),
        CONSTRAINT ck_evidencia_tamanho CHECK (tamanho > 0),
        CONSTRAINT ck_evidencia_sha256 CHECK (length(sha256) = 32)
    )
    """,
    "CREATE INDEX ix_evidencia_tenant_id_fonte_id ON evidencia (tenant_id, fonte_id)",
    # O lancamento so entra em inventario em coleta, em fonte ativa e no periodo que a
    # periodicidade da fonte permite. Roda com os privilegios (e o RLS) de quem insere.
    """
    CREATE FUNCTION lancamento_checar() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    DECLARE
        v_periodicidade text;
        v_ativa boolean;
        v_estado text;
    BEGIN
        SELECT f.periodicidade, f.ativa, i.estado
          INTO v_periodicidade, v_ativa, v_estado
          FROM fonte_emissao f
          JOIN inventario i
            ON i.tenant_id = f.tenant_id AND i.inventario_id = f.inventario_id
         WHERE f.tenant_id = NEW.tenant_id AND f.fonte_id = NEW.fonte_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'fonte nao encontrada' USING ERRCODE = 'CL010';
        END IF;
        IF v_estado <> 'em_coleta' THEN
            RAISE EXCEPTION 'inventario fora da coleta' USING ERRCODE = 'CL011';
        END IF;
        IF NOT v_ativa THEN
            RAISE EXCEPTION 'fonte desativada' USING ERRCODE = 'CL012';
        END IF;
        IF (v_periodicidade = 'anual') <> (NEW.periodo = 0) THEN
            RAISE EXCEPTION 'periodo incompativel com a periodicidade' USING ERRCODE = 'CL013';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    # Segregacao de funcoes: quem lancou nao decide; e so a versao mais recente e decidida.
    """
    CREATE FUNCTION lancamento_decisao_checar() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    DECLARE
        v_lanc lancamento%ROWTYPE;
    BEGIN
        SELECT * INTO v_lanc FROM lancamento
         WHERE tenant_id = NEW.tenant_id AND lancamento_id = NEW.lancamento_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'lancamento nao encontrado' USING ERRCODE = 'CL010';
        END IF;
        IF v_lanc.empresa_id <> NEW.empresa_id THEN
            RAISE EXCEPTION 'empresa diferente da do lancamento' USING ERRCODE = 'CL010';
        END IF;
        IF v_lanc.criado_por IS NOT DISTINCT FROM NEW.decidido_por THEN
            RAISE EXCEPTION 'quem lancou nao decide' USING ERRCODE = 'CL014';
        END IF;
        IF EXISTS (
            SELECT 1 FROM lancamento
             WHERE tenant_id = v_lanc.tenant_id AND fonte_id = v_lanc.fonte_id
               AND periodo = v_lanc.periodo AND versao > v_lanc.versao
        ) THEN
            RAISE EXCEPTION 'existe versao mais recente' USING ERRCODE = 'CL015';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION lancamento_checar() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION lancamento_decisao_checar() FROM PUBLIC",
    "CREATE TRIGGER lancamento_checar BEFORE INSERT ON lancamento"
    " FOR EACH ROW EXECUTE FUNCTION lancamento_checar()",
    "CREATE TRIGGER lancamento_decisao_checar BEFORE INSERT ON lancamento_decisao"
    " FOR EACH ROW EXECUTE FUNCTION lancamento_decisao_checar()",
]

TABELAS = (
    # (tabela, por_empresa, privilegios do app_user)
    ("inventario", True, ["SELECT", "INSERT", "UPDATE"]),
    ("fonte_emissao", True, ["SELECT", "INSERT", "UPDATE"]),
    ("planilha_importacao", True, ["SELECT", "INSERT"]),
    ("lancamento", True, ["SELECT", "INSERT"]),
    ("lancamento_decisao", True, ["SELECT", "INSERT"]),
    ("evidencia", True, ["SELECT", "INSERT"]),
)


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela, por_empresa, privilegios in TABELAS:
        for comando in sql_rls_tabela(tabela, por_empresa=por_empresa):
            op.execute(comando)
        for comando in sql_conceder(tabela, privilegios):
            op.execute(comando)
    # somente_insercao() vem da migracao 0005: recusa UPDATE, DELETE e TRUNCATE, ate do dono.
    for tabela in SO_INSERCAO:
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_alteracao BEFORE UPDATE OR DELETE ON {tabela}"
            " FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
        )
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_truncate BEFORE TRUNCATE ON {tabela}"
            " FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
        )


def downgrade() -> None:
    for tabela in (
        "evidencia",
        "lancamento_decisao",
        "lancamento",
        "planilha_importacao",
        "fonte_emissao",
        "inventario",
    ):
        op.execute(f"DROP TABLE {tabela}")
    op.execute("DROP FUNCTION lancamento_decisao_checar()")
    op.execute("DROP FUNCTION lancamento_checar()")
    op.execute("ALTER TABLE unidade DROP CONSTRAINT uq_unidade_tenant_id_unidade_id_empresa_id")
