"""Trilha de auditoria: audit_log so de insercao, com hash encadeado por tenant.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-20

A trigger audit_encadear calcula seq, hash_anterior, ocorrido_em e hash; o que a aplicacao
mandar nesses campos e descartado. O lock consultivo por tenant serializa a cadeia.
A trigger audit_imutavel recusa UPDATE, DELETE e TRUNCATE, inclusive do dono da tabela.

Conteudo do hash (versao v1), campos separados por "|":
    v1|tenant_id|seq|evento_id|ocorrido_em UTC (AAAA-MM-DDTHH:MM:SS.ffffffZ)|usuario_id ou
    vazio|acao|entidade|entidade_id ou vazio|dados (texto do jsonb)
hash = SHA-256(hash_anterior || conteudo em UTF-8); o primeiro hash_anterior e 32 bytes zero.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMANDOS = [
    """
    CREATE TABLE audit_log (
        tenant_id uuid NOT NULL,
        seq bigint NOT NULL,
        evento_id uuid NOT NULL,
        ocorrido_em timestamptz NOT NULL,
        usuario_id uuid,
        acao text NOT NULL,
        entidade text NOT NULL,
        entidade_id text,
        dados jsonb NOT NULL DEFAULT '{}',
        hash_anterior bytea NOT NULL,
        hash bytea NOT NULL,
        CONSTRAINT pk_audit_log PRIMARY KEY (tenant_id, seq),
        CONSTRAINT uq_audit_log_tenant_id_evento_id UNIQUE (tenant_id, evento_id),
        CONSTRAINT fk_audit_log_tenant_id_tenant FOREIGN KEY (tenant_id)
            REFERENCES tenant (tenant_id),
        CONSTRAINT ck_audit_log_acao CHECK (acao ~ '^[a-z][a-z_]*\\.[a-z][a-z_]*$'),
        CONSTRAINT ck_audit_log_entidade CHECK (entidade ~ '^[a-z][a-z_]*$'),
        CONSTRAINT ck_audit_log_entidade_id CHECK (entidade_id IS NULL OR entidade_id !~ '[|]'),
        CONSTRAINT ck_audit_log_dados_objeto CHECK (jsonb_typeof(dados) = 'object'),
        CONSTRAINT ck_audit_log_hash CHECK (
            octet_length(hash) = 32 AND octet_length(hash_anterior) = 32
        )
    )
    """,
    """
    CREATE FUNCTION audit_encadear() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    DECLARE
        v_seq bigint;
        v_hash bytea;
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('audit_log:' || NEW.tenant_id::text, 0));
        SELECT a.seq, a.hash INTO v_seq, v_hash
        FROM audit_log a WHERE a.tenant_id = NEW.tenant_id
        ORDER BY a.seq DESC LIMIT 1;

        NEW.seq := coalesce(v_seq, 0) + 1;
        NEW.hash_anterior := coalesce(v_hash, decode(repeat('00', 32), 'hex'));
        NEW.evento_id := gen_random_uuid();
        NEW.ocorrido_em := clock_timestamp();
        NEW.dados := coalesce(NEW.dados, '{}'::jsonb);
        NEW.hash := sha256(
            NEW.hash_anterior || convert_to(
                'v1|' || NEW.tenant_id::text
                || '|' || NEW.seq::text
                || '|' || NEW.evento_id::text
                || '|' || to_char(NEW.ocorrido_em AT TIME ZONE 'UTC',
                                  'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                || '|' || coalesce(NEW.usuario_id::text, '')
                || '|' || NEW.acao
                || '|' || NEW.entidade
                || '|' || coalesce(NEW.entidade_id, '')
                || '|' || NEW.dados::text,
                'UTF8'
            )
        );
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE FUNCTION audit_imutavel() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log so aceita insercao' USING ERRCODE = 'CL002';
    END
    $$
    """,
    """
    CREATE TRIGGER audit_log_encadear BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_encadear()
    """,
    """
    CREATE TRIGGER audit_log_sem_alteracao BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_imutavel()
    """,
    """
    CREATE TRIGGER audit_log_sem_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_imutavel()
    """,
    "REVOKE ALL ON FUNCTION audit_encadear() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION audit_imutavel() FROM PUBLIC",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for comando in sql_rls_tabela("audit_log", por_empresa=False):
        op.execute(comando)
    # So insercao e leitura; nunca UPDATE nem DELETE.
    for comando in sql_conceder("audit_log", ["SELECT", "INSERT"]):
        op.execute(comando)


def downgrade() -> None:
    # Desfazer a migracao apaga a trilha: so faz sentido em banco de desenvolvimento.
    op.execute("DROP TABLE audit_log")
    op.execute("DROP FUNCTION audit_imutavel()")
    op.execute("DROP FUNCTION audit_encadear()")
