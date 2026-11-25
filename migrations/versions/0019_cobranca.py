"""Cobranca por empresa ativa por mes (P22, com gateway simulado; Asaas depois).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-25

- assinatura: uma por tenant (teste, ativa, inadimplente ou cancelada), com o fim do periodo
  de teste e o id do cliente no gateway.
- fatura: uma por tenant e competencia (mes), com as empresas ativas contadas, o valor em
  centavos e o detalhamento das faixas. O app so muda situacao, pagamento e os dados do
  gateway (privilegio por coluna); valor e competencia nao mudam depois de gravados.
- cobranca_evento: cada evento do gateway processado, uma vez so (chave primaria tenant,
  gateway e id do evento; um evento sempre leva ao mesmo tenant): repeticao do webhook nao faz
  nada. So insercao.
- cobranca_localizar(gateway, cobranca): o webhook chega sem sessao e sem tenant; esta funcao
  do app_identidade devolve so o tenant e a fatura daquela cobranca. Revisao de seguranca:
  SECURITY DEFINER, search_path fixo, so leitura, EXECUTE so para o app_user, nenhum dado alem
  dos dois ids; o processamento depois roda com o contexto do tenant (RLS).
O verificador nao ve nada daqui.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINIDOR = "SECURITY DEFINER SET search_path = pg_catalog, public"

COMANDOS = [
    """
    CREATE TABLE assinatura (
        tenant_id uuid NOT NULL,
        situacao text NOT NULL,
        teste_ate date NOT NULL,
        gateway text NOT NULL,
        gateway_cliente_id text,
        criada_em timestamptz NOT NULL DEFAULT now(),
        atualizada_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_assinatura PRIMARY KEY (tenant_id),
        CONSTRAINT fk_assinatura_tenant FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
        CONSTRAINT ck_assinatura_situacao CHECK (
            situacao IN ('teste', 'ativa', 'inadimplente', 'cancelada')
        ),
        CONSTRAINT ck_assinatura_gateway CHECK (gateway IN ('simulado', 'asaas'))
    )
    """,
    """
    CREATE TABLE fatura (
        tenant_id uuid NOT NULL,
        fatura_id uuid NOT NULL,
        competencia date NOT NULL,
        empresas_ativas integer NOT NULL,
        valor_centavos bigint NOT NULL,
        detalhamento jsonb NOT NULL,
        vencimento date NOT NULL,
        situacao text NOT NULL DEFAULT 'aberta',
        gateway text NOT NULL,
        gateway_cobranca_id text,
        url_pagamento text,
        criada_em timestamptz NOT NULL DEFAULT now(),
        paga_em timestamptz,
        CONSTRAINT pk_fatura PRIMARY KEY (tenant_id, fatura_id),
        CONSTRAINT uq_fatura_tenant_id_competencia UNIQUE (tenant_id, competencia),
        CONSTRAINT fk_fatura_assinatura FOREIGN KEY (tenant_id) REFERENCES assinatura (tenant_id),
        CONSTRAINT ck_fatura_competencia CHECK (extract(day FROM competencia) = 1),
        CONSTRAINT ck_fatura_valores CHECK (empresas_ativas >= 0 AND valor_centavos >= 0),
        CONSTRAINT ck_fatura_situacao CHECK (situacao IN ('aberta', 'paga', 'vencida', 'cancelada')),
        CONSTRAINT ck_fatura_paga CHECK ((situacao = 'paga') = (paga_em IS NOT NULL)),
        CONSTRAINT ck_fatura_gateway CHECK (gateway IN ('simulado', 'asaas'))
    )
    """,
    # Indices comecam por tenant_id (regra do projeto). O id da cobranca e unico no gateway e
    # sempre leva ao mesmo tenant: a unicidade por tenant basta.
    "CREATE UNIQUE INDEX uq_fatura_tenant_id_gateway_cobranca ON fatura"
    " (tenant_id, gateway, gateway_cobranca_id) WHERE gateway_cobranca_id IS NOT NULL",
    "CREATE INDEX ix_fatura_tenant_id_situacao ON fatura (tenant_id, situacao, vencimento)",
    """
    CREATE TABLE cobranca_evento (
        tenant_id uuid NOT NULL,
        evento_id text NOT NULL,
        gateway text NOT NULL,
        tipo text NOT NULL,
        fatura_id uuid NOT NULL,
        recebido_em timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT pk_cobranca_evento PRIMARY KEY (tenant_id, gateway, evento_id),
        CONSTRAINT fk_cobranca_evento_fatura FOREIGN KEY (tenant_id, fatura_id)
            REFERENCES fatura (tenant_id, fatura_id)
    )
    """,
    f"""
    CREATE FUNCTION cobranca_localizar(p_gateway text, p_cobranca text)
    RETURNS TABLE (tenant_id uuid, fatura_id uuid)
    LANGUAGE sql STABLE {DEFINIDOR} AS $$
        SELECT f.tenant_id, f.fatura_id FROM fatura f
         WHERE f.gateway = p_gateway AND f.gateway_cobranca_id = p_cobranca
    $$
    """,
    "REVOKE ALL ON FUNCTION cobranca_localizar(text, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION cobranca_localizar(text, text) TO app_user",
    "GRANT SELECT ON fatura TO app_identidade",
    "ALTER FUNCTION cobranca_localizar(text, text) OWNER TO app_identidade",
]


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for tabela, privilegios in (
        ("assinatura", ["SELECT", "INSERT", "UPDATE"]),
        ("fatura", ["SELECT", "INSERT"]),
        ("cobranca_evento", ["SELECT", "INSERT"]),
    ):
        for comando in sql_rls_tabela(tabela, por_empresa=False):
            op.execute(comando)
        for comando in sql_conceder(tabela, privilegios):
            op.execute(comando)
        op.execute(
            f"CREATE POLICY {tabela}_verificador ON {tabela} AS RESTRICTIVE FOR SELECT"
            " TO app_verificador USING (false)"
        )
    # Fatura: o app so muda situacao, pagamento e os dados do gateway.
    op.execute(
        "GRANT UPDATE (situacao, paga_em, gateway_cobranca_id, url_pagamento) ON fatura"
        " TO app_user"
    )
    op.execute(
        "CREATE TRIGGER cobranca_evento_sem_alteracao BEFORE UPDATE OR DELETE ON cobranca_evento"
        " FOR EACH ROW EXECUTE FUNCTION somente_insercao()"
    )
    for tabela in ("assinatura", "fatura", "cobranca_evento"):
        op.execute(
            f"CREATE TRIGGER {tabela}_sem_truncate BEFORE TRUNCATE ON {tabela}"
            " FOR EACH STATEMENT EXECUTE FUNCTION somente_insercao()"
        )


def downgrade() -> None:
    op.execute("SET LOCAL ROLE app_identidade")
    op.execute("DROP FUNCTION cobranca_localizar(text, text)")
    op.execute("RESET ROLE")
    op.execute("REVOKE SELECT ON fatura FROM app_identidade")
    op.execute("DROP TABLE cobranca_evento")
    op.execute("DROP TABLE fatura")
    op.execute("DROP TABLE assinatura")
