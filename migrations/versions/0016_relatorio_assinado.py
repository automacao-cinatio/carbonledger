"""Relatorio do inventario em PDF, assinado com o certificado da propria empresa (P19).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-24

- certificado_assinatura: o certificado A1 de cada empresa (decisao do usuario: cada cliente
  assina com o proprio certificado). A chave privada e o certificado ficam so cifrados (cifra
  envelope de app/core/keys.py, contexto com tenant, empresa e certificado); a senha do .pfx
  nunca e guardada. Um ativo por empresa (indice unico parcial). Desativar e a unica mudanca e
  apaga a chave cifrada (sem chave, ninguem mais assina com ela); os dados publicos do
  certificado ficam, porque os relatorios antigos apontam para ele.
- relatorio_inventario: cada PDF assinado de um fechamento, com o hash do fechamento que ele
  retrata, o SHA-256 do arquivo e o objeto no S3 (Object Lock). So insercao.
O verificador nao ve nada daqui por enquanto.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMANDOS = [
    """
    CREATE TABLE certificado_assinatura (
        tenant_id uuid NOT NULL,
        certificado_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        titular text NOT NULL,
        cnpj text,
        emissor text NOT NULL,
        numero_serie text NOT NULL,
        valido_de timestamptz NOT NULL,
        valido_ate timestamptz NOT NULL,
        impressao_sha256 bytea NOT NULL,
        chave_cifrada text,
        enviado_por uuid NOT NULL,
        enviado_em timestamptz NOT NULL DEFAULT now(),
        desativado_em timestamptz,
        desativado_por uuid,
        CONSTRAINT pk_certificado_assinatura PRIMARY KEY (tenant_id, certificado_id),
        CONSTRAINT uq_certificado_assinatura_tenant_id_certificado_id_empresa_id
            UNIQUE (tenant_id, certificado_id, empresa_id),
        CONSTRAINT fk_certificado_assinatura_empresa FOREIGN KEY (tenant_id, empresa_id)
            REFERENCES empresa (tenant_id, empresa_id),
        CONSTRAINT ck_certificado_assinatura_cnpj CHECK (cnpj IS NULL OR cnpj ~ '^[0-9]{14}$'),
        CONSTRAINT ck_certificado_assinatura_validade CHECK (valido_ate > valido_de),
        CONSTRAINT ck_certificado_assinatura_impressao CHECK (length(impressao_sha256) = 32),
        CONSTRAINT ck_certificado_assinatura_desativacao CHECK (
            (desativado_em IS NULL) = (desativado_por IS NULL)
        ),
        -- Ativo tem a chave; desativado nao tem mais.
        CONSTRAINT ck_certificado_assinatura_chave CHECK (
            (desativado_em IS NULL) = (chave_cifrada IS NOT NULL)
        )
    )
    """,
    "CREATE UNIQUE INDEX uq_certificado_assinatura_tenant_id_empresa_id_ativo"
    " ON certificado_assinatura (tenant_id, empresa_id) WHERE desativado_em IS NULL",
    """
    CREATE TABLE relatorio_inventario (
        tenant_id uuid NOT NULL,
        relatorio_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        versao_fechamento integer NOT NULL,
        hash_fechamento bytea NOT NULL,
        certificado_id uuid NOT NULL,
        sha256 bytea NOT NULL,
        tamanho integer NOT NULL,
        chave_objeto text NOT NULL,
        versao_objeto text,
        gerado_por uuid NOT NULL,
        gerado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_relatorio_inventario PRIMARY KEY (tenant_id, relatorio_id),
        CONSTRAINT fk_relatorio_inventario_inventario
            FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT fk_relatorio_inventario_certificado
            FOREIGN KEY (tenant_id, certificado_id, empresa_id)
            REFERENCES certificado_assinatura (tenant_id, certificado_id, empresa_id),
        CONSTRAINT ck_relatorio_inventario_versao CHECK (versao_fechamento >= 1),
        CONSTRAINT ck_relatorio_inventario_hashes CHECK (
            length(hash_fechamento) = 32 AND length(sha256) = 32
        ),
        CONSTRAINT ck_relatorio_inventario_tamanho CHECK (tamanho > 0)
    )
    """,
    "CREATE INDEX ix_relatorio_inventario_tenant_id_inventario_id"
    " ON relatorio_inventario (tenant_id, inventario_id, gerado_em)",
    "CREATE INDEX ix_relatorio_inventario_tenant_id_empresa_id"
    " ON relatorio_inventario (tenant_id, empresa_id, gerado_em)",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela, privilegios in (
        ("certificado_assinatura", ["SELECT", "INSERT", "UPDATE"]),
        ("relatorio_inventario", ["SELECT", "INSERT"]),
    ):
        for comando in sql_rls_tabela(tabela, por_empresa=True):
            op.execute(comando)
        for comando in sql_conceder(tabela, privilegios):
            op.execute(comando)
        op.execute(
            f"CREATE POLICY {tabela}_verificador ON {tabela} AS RESTRICTIVE FOR SELECT"
            " TO app_verificador USING (false)"
        )
    # O app_user so desativa: nenhuma outra coluna do certificado muda depois de gravada.
    op.execute("REVOKE UPDATE ON certificado_assinatura FROM app_user")
    op.execute(
        "GRANT UPDATE (chave_cifrada, desativado_em, desativado_por)"
        " ON certificado_assinatura TO app_user"
    )
    op.execute(
        """
        CREATE FUNCTION certificado_assinatura_so_desativa() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.desativado_em IS NOT NULL THEN
                RAISE EXCEPTION 'certificado ja desativado' USING ERRCODE = 'CL040';
            END IF;
            IF NEW.desativado_em IS NULL OR NEW.chave_cifrada IS NOT NULL THEN
                RAISE EXCEPTION 'certificado so pode ser desativado' USING ERRCODE = 'CL040';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER certificado_assinatura_so_desativa BEFORE UPDATE ON"
        " certificado_assinatura FOR EACH ROW EXECUTE FUNCTION certificado_assinatura_so_desativa()"
    )
    op.execute(
        "CREATE TRIGGER certificado_assinatura_sem_delete BEFORE DELETE ON certificado_assinatura"
        " FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
    )
    op.execute(
        "CREATE TRIGGER relatorio_inventario_sem_alteracao BEFORE UPDATE OR DELETE ON"
        " relatorio_inventario FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
    )
    for tabela in ("certificado_assinatura", "relatorio_inventario"):
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_truncate BEFORE TRUNCATE ON {tabela}"
            " FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE relatorio_inventario")
    op.execute("DROP TABLE certificado_assinatura")
    op.execute("DROP FUNCTION certificado_assinatura_so_desativa()")
