"""Estrutura: grupo, empresa completa e unidade.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20

Hierarquia dentro do tenant: grupo, empresa (CNPJ da matriz) e unidade (estabelecimento ou
instalacao). A unidade guarda os dados dos limites organizacionais do GHG Protocol:
participacao societaria (%) e se a empresa tem controle operacional e financeiro. A
abordagem de consolidacao e escolhida em cada inventario.

Nada some: empresa e unidade so se desativam (sem DELETE para o app_user).
CNPJ alfanumerico (Receita Federal, a partir de julho de 2026): 12 posicoes de 0-9 ou A-Z e
2 digitos verificadores. O banco confere o formato; o digito verificador e conferido no app.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UFS = (
    "('AC','AL','AM','AP','BA','CE','DF','ES','GO','MA','MG','MS','MT','PA','PB','PE','PI',"
    "'PR','RJ','RN','RO','RR','RS','SC','SE','SP','TO')"
)
FORMATO_CNPJ = "'^[0-9A-Z]{12}[0-9]{2}$'"

COMANDOS = [
    """
    CREATE TABLE grupo (
        tenant_id uuid NOT NULL,
        grupo_id uuid NOT NULL,
        nome text NOT NULL,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_grupo PRIMARY KEY (tenant_id, grupo_id),
        CONSTRAINT uq_grupo_tenant_id_nome UNIQUE (tenant_id, nome),
        CONSTRAINT fk_grupo_tenant_id_tenant FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
        CONSTRAINT ck_grupo_nome CHECK (length(btrim(nome)) BETWEEN 1 AND 200)
    )
    """,
    # Empresas que ja existem vao para um grupo padrao do proprio tenant. O dono da tabela so
    # enxerga todas as linhas sem o FORCE; ele volta logo abaixo, na mesma transacao.
    "ALTER TABLE empresa NO FORCE ROW LEVEL SECURITY",
    """
    INSERT INTO grupo (tenant_id, grupo_id, nome)
    SELECT t.tenant_id, gen_random_uuid(), 'Grupo principal'
    FROM (SELECT DISTINCT tenant_id FROM empresa) AS t
    """,
    "ALTER TABLE empresa ADD COLUMN grupo_id uuid",
    "UPDATE empresa e SET grupo_id = g.grupo_id FROM grupo g WHERE g.tenant_id = e.tenant_id",
    "ALTER TABLE empresa ALTER COLUMN grupo_id SET NOT NULL",
    "ALTER TABLE empresa FORCE ROW LEVEL SECURITY",
    f"""
    ALTER TABLE empresa
        ADD COLUMN cnpj text,
        ADD COLUMN nome_fantasia text,
        ADD COLUMN cnae text,
        ADD COLUMN uf text,
        ADD COLUMN municipio text,
        ADD COLUMN ativa boolean NOT NULL DEFAULT true,
        ADD COLUMN atualizado_em timestamptz NOT NULL DEFAULT now(),
        ADD CONSTRAINT fk_empresa_grupo FOREIGN KEY (tenant_id, grupo_id)
            REFERENCES grupo (tenant_id, grupo_id),
        ADD CONSTRAINT ck_empresa_cnpj CHECK (cnpj IS NULL OR cnpj ~ {FORMATO_CNPJ}),
        ADD CONSTRAINT ck_empresa_cnae CHECK (cnae IS NULL OR cnae ~ '^[0-9]{{4}}-[0-9]/[0-9]{{2}}$'),
        ADD CONSTRAINT ck_empresa_uf CHECK (uf IS NULL OR uf IN {UFS}),
        ADD CONSTRAINT ck_empresa_nome CHECK (length(btrim(nome)) BETWEEN 1 AND 200)
    """,
    "CREATE UNIQUE INDEX uq_empresa_tenant_id_cnpj ON empresa (tenant_id, cnpj) WHERE cnpj IS NOT NULL",
    "CREATE INDEX ix_empresa_tenant_id_grupo_id ON empresa (tenant_id, grupo_id)",
    f"""
    CREATE TABLE unidade (
        tenant_id uuid NOT NULL,
        unidade_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        nome text NOT NULL,
        cnpj text,
        uf text NOT NULL,
        municipio text NOT NULL,
        participacao_societaria numeric(7, 4) NOT NULL DEFAULT 100,
        controle_operacional boolean NOT NULL,
        controle_financeiro boolean NOT NULL,
        ativa boolean NOT NULL DEFAULT true,
        criado_em timestamptz NOT NULL DEFAULT now(),
        atualizado_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_unidade PRIMARY KEY (tenant_id, unidade_id),
        CONSTRAINT fk_unidade_empresa FOREIGN KEY (tenant_id, empresa_id)
            REFERENCES empresa (tenant_id, empresa_id),
        CONSTRAINT uq_unidade_tenant_id_empresa_id_nome UNIQUE (tenant_id, empresa_id, nome),
        CONSTRAINT ck_unidade_nome CHECK (length(btrim(nome)) BETWEEN 1 AND 200),
        CONSTRAINT ck_unidade_cnpj CHECK (cnpj IS NULL OR cnpj ~ {FORMATO_CNPJ}),
        CONSTRAINT ck_unidade_uf CHECK (uf IN {UFS}),
        CONSTRAINT ck_unidade_municipio CHECK (length(btrim(municipio)) BETWEEN 1 AND 200),
        CONSTRAINT ck_unidade_participacao CHECK (
            participacao_societaria >= 0 AND participacao_societaria <= 100
        )
    )
    """,
    "CREATE UNIQUE INDEX uq_unidade_tenant_id_cnpj ON unidade (tenant_id, cnpj) WHERE cnpj IS NOT NULL",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela, por_empresa in (("grupo", False), ("unidade", True)):
        for comando in sql_rls_tabela(tabela, por_empresa=por_empresa):
            op.execute(comando)
        for comando in sql_conceder(tabela, ["SELECT", "INSERT", "UPDATE"]):
            op.execute(comando)


def downgrade() -> None:
    op.execute("DROP TABLE unidade")
    op.execute(
        "ALTER TABLE empresa DROP COLUMN cnpj, DROP COLUMN nome_fantasia, DROP COLUMN cnae,"
        " DROP COLUMN uf, DROP COLUMN municipio, DROP COLUMN ativa, DROP COLUMN atualizado_em,"
        " DROP CONSTRAINT ck_empresa_nome, DROP COLUMN grupo_id"
    )
    op.execute("DROP TABLE grupo")
