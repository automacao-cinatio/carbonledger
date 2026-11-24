"""Fechamento e reapresentacao do inventario.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-23

- inventario.estado ganha 'fechado'.
- inventario_evento: fechamento e reabertura (reapresentacao), so de insercao. O fechamento
  guarda a versao (1 no primeiro, 2 na primeira reapresentacao...), o calculo usado e o hash
  do fechamento; a reabertura guarda o motivo.
- O estado do inventario so muda com o evento correspondente gravado na mesma transacao
  (trigger inventario_checar_estado). Fechado, o inventario nao muda em mais nada.
- Com o inventario fechado, nada entra nem muda na coleta e no calculo dele: fonte,
  lancamento, decisao, evidencia, planilha e calculo (trigger coleta_exige_inventario_aberto).
  A trigger lancamento_checar da 0006 ja recusava lancamento fora da coleta.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tabela -> como achar o inventario a partir de NEW.
ACHAR_INVENTARIO = {
    "fonte_emissao": "NEW.inventario_id",
    "planilha_importacao": "NEW.inventario_id",
    "calculo": "NEW.inventario_id",
    "evidencia": (
        "(SELECT inventario_id FROM fonte_emissao"
        " WHERE tenant_id = NEW.tenant_id AND fonte_id = NEW.fonte_id)"
    ),
    "lancamento_decisao": (
        "(SELECT f.inventario_id FROM lancamento l JOIN fonte_emissao f"
        " ON f.tenant_id = l.tenant_id AND f.fonte_id = l.fonte_id"
        " WHERE l.tenant_id = NEW.tenant_id AND l.lancamento_id = NEW.lancamento_id)"
    ),
}

COMANDOS = [
    "ALTER TABLE inventario DROP CONSTRAINT ck_inventario_estado",
    "ALTER TABLE inventario ADD CONSTRAINT ck_inventario_estado"
    " CHECK (estado IN ('em_coleta', 'fechado'))",
    """
    CREATE TABLE inventario_evento (
        tenant_id uuid NOT NULL,
        evento_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        tipo text NOT NULL,
        versao integer NOT NULL,
        calculo_id uuid,
        hash_fechamento bytea,
        motivo text,
        transacao bigint NOT NULL DEFAULT txid_current(),
        registrado_por uuid NOT NULL,
        -- clock_timestamp: a ordem dos eventos vale ate dentro da mesma transacao.
        registrado_em timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_inventario_evento PRIMARY KEY (tenant_id, evento_id),
        CONSTRAINT uq_inventario_evento_tenant_id_inventario_id_tipo_versao
            UNIQUE (tenant_id, inventario_id, tipo, versao),
        CONSTRAINT fk_inventario_evento_inventario
            FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT fk_inventario_evento_calculo FOREIGN KEY (tenant_id, calculo_id, empresa_id)
            REFERENCES calculo (tenant_id, calculo_id, empresa_id),
        CONSTRAINT ck_inventario_evento_tipo CHECK (tipo IN ('fechamento', 'reabertura')),
        CONSTRAINT ck_inventario_evento_versao CHECK (versao >= 1),
        CONSTRAINT ck_inventario_evento_fechamento CHECK (
            tipo <> 'fechamento'
            OR (calculo_id IS NOT NULL AND length(hash_fechamento) = 32 AND motivo IS NULL)
        ),
        CONSTRAINT ck_inventario_evento_reabertura CHECK (
            tipo <> 'reabertura'
            OR (calculo_id IS NULL AND hash_fechamento IS NULL
                AND length(btrim(coalesce(motivo, ''))) BETWEEN 20 AND 1000)
        )
    )
    """,
    """
    CREATE FUNCTION inventario_checar_estado() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    BEGIN
        IF OLD.estado = 'fechado' AND NEW.estado = 'fechado' THEN
            RAISE EXCEPTION 'inventario fechado: so muda por reapresentacao'
                USING ERRCODE = 'CL020';
        END IF;
        IF NEW.estado IS DISTINCT FROM OLD.estado AND NOT EXISTS (
            SELECT 1 FROM inventario_evento e
             WHERE e.tenant_id = NEW.tenant_id AND e.inventario_id = NEW.inventario_id
               AND e.transacao = txid_current()
               AND e.tipo = CASE NEW.estado WHEN 'fechado' THEN 'fechamento' ELSE 'reabertura' END
        ) THEN
            RAISE EXCEPTION 'mudanca de estado sem o evento na mesma transacao'
                USING ERRCODE = 'CL021';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE FUNCTION inventario_aberto(p_tenant uuid, p_inventario uuid) RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
        SELECT coalesce(
            (SELECT estado = 'em_coleta' FROM inventario
              WHERE tenant_id = p_tenant AND inventario_id = p_inventario),
            true
        )
    $$
    """,
    "REVOKE ALL ON FUNCTION inventario_checar_estado() FROM PUBLIC",
    "CREATE TRIGGER inventario_checar_estado BEFORE UPDATE ON inventario"
    " FOR EACH ROW EXECUTE FUNCTION inventario_checar_estado()",
]


def _sql_trava(tabela: str, achar: str) -> list[str]:
    funcao = f"{tabela}_exige_inventario_aberto"
    return [
        f"""
        CREATE FUNCTION {funcao}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT inventario_aberto(NEW.tenant_id, {achar}) THEN
                RAISE EXCEPTION 'inventario fechado: so muda por reapresentacao'
                    USING ERRCODE = 'CL020';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        f"REVOKE ALL ON FUNCTION {funcao}() FROM PUBLIC",
        f"CREATE TRIGGER {funcao} BEFORE INSERT OR UPDATE ON {tabela}"
        f" FOR EACH ROW EXECUTE FUNCTION {funcao}()",
    ]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for comando in sql_rls_tabela("inventario_evento", por_empresa=True):
        op.execute(comando)
    for comando in sql_conceder("inventario_evento", ["SELECT", "INSERT"]):
        op.execute(comando)
    op.execute(
        "CREATE TRIGGER inventario_evento_sem_alteracao BEFORE UPDATE OR DELETE ON"
        " inventario_evento FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
    )
    op.execute(
        "CREATE TRIGGER inventario_evento_sem_truncate BEFORE TRUNCATE ON inventario_evento"
        " FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
    )
    for tabela, achar in ACHAR_INVENTARIO.items():
        for comando in _sql_trava(tabela, achar):
            op.execute(comando)


def downgrade() -> None:
    for tabela in ACHAR_INVENTARIO:
        op.execute(f"DROP TRIGGER {tabela}_exige_inventario_aberto ON {tabela}")
        op.execute(f"DROP FUNCTION {tabela}_exige_inventario_aberto()")
    op.execute("DROP TRIGGER inventario_checar_estado ON inventario")
    op.execute("DROP FUNCTION inventario_checar_estado()")
    op.execute("DROP FUNCTION inventario_aberto(uuid, uuid)")
    op.execute("DROP TABLE inventario_evento")
    op.execute("ALTER TABLE inventario DROP CONSTRAINT ck_inventario_estado")
    op.execute(
        "ALTER TABLE inventario ADD CONSTRAINT ck_inventario_estado CHECK (estado IN ('em_coleta'))"
    )
