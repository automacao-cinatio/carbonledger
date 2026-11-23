"""Verificador: concessao de acesso a um inventario, limitada no banco.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-23

- concessao_verificacao: o administrador concede a um e-mail o acesso de leitura a um
  inventario fechado, com prazo. O verificador aceita pelo link (OIDC), uma vez. A concessao
  pode ser revogada a qualquer momento; nada se apaga.
- Contexto novo, por transacao: app.concessao_id. Com ele, a aplicacao faz SET LOCAL ROLE
  app_verificador (so SELECT, desde o P02).
- Politica restritiva TO app_verificador em toda tabela de tenant: o verificador ve so o
  inventario da concessao (e o que pende dele), a empresa e as unidades dela, e nada de
  membros, convites, trilha ou outras concessoes. Concessao vencida, revogada ou nao aceita
  nao abre nada (verificacao_inventario() devolve NULL).
- Duas funcoes do app_identidade (SECURITY DEFINER), para antes de haver contexto:
  identidade_aceitar_concessao (aceite pelo link) e identidade_concessoes (concessoes ativas
  da pessoa). Revisao de seguranca: as duas so leem ou gravam concessao_verificacao, usuario e
  identidade, conferem o hash do segredo, o e-mail, o prazo e a revogacao, e usam um unico
  erro (CL001) para todo motivo de recusa, como o aceite de convite.
- O verificador le a referencia global (fatores, GWP, misturas) para conferir a memoria de
  calculo.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import sql_conceder, sql_rls_tabela

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAPEL_APP = "app_user"
PAPEL_VERIFICADOR = "app_verificador"
PAPEL_IDENTIDADE = "app_identidade"
DEFINIDOR = "SECURITY DEFINER SET search_path = pg_catalog, public"
REFERENCIA = ("lote_importacao", "lote_decisao", "gwp_versao", "fator_versao", "mistura_versao")

POR_INVENTARIO = "inventario_id = (SELECT verificacao_inventario())"
# Tabela -> o que o verificador ve nela (alem do tenant e das empresas do contexto).
VISAO_DO_VERIFICADOR = {
    "tenant": "true",
    "empresa": "empresa_id = (SELECT verificacao_empresa())",
    "grupo": "grupo_id IN (SELECT e.grupo_id FROM empresa e)",
    "unidade": "empresa_id = (SELECT verificacao_empresa())",
    "membro": "false",
    "membro_empresa": "false",
    "convite": "false",
    "audit_log": "false",
    "inventario": POR_INVENTARIO,
    "fonte_emissao": POR_INVENTARIO,
    "planilha_importacao": POR_INVENTARIO,
    "calculo": POR_INVENTARIO,
    "inventario_evento": POR_INVENTARIO,
    "lancamento": "fonte_id IN (SELECT f.fonte_id FROM fonte_emissao f)",
    "evidencia": "fonte_id IN (SELECT f.fonte_id FROM fonte_emissao f)",
    "lancamento_decisao": "lancamento_id IN (SELECT l.lancamento_id FROM lancamento l)",
    "calculo_linha": "calculo_id IN (SELECT c.calculo_id FROM calculo c)",
    "calculo_pendencia": "calculo_id IN (SELECT c.calculo_id FROM calculo c)",
    "concessao_verificacao": "concessao_id = (SELECT contexto_concessao())",
}

COMANDOS = [
    """
    CREATE TABLE concessao_verificacao (
        tenant_id uuid NOT NULL,
        concessao_id uuid NOT NULL,
        empresa_id uuid NOT NULL,
        inventario_id uuid NOT NULL,
        email text NOT NULL,
        segredo_hash bytea NOT NULL,
        criado_por uuid NOT NULL,
        criado_em timestamptz NOT NULL DEFAULT now(),
        expira_em timestamptz NOT NULL,
        aceito_em timestamptz,
        usuario_id uuid,
        revogado_em timestamptz,
        revogado_por uuid,
        CONSTRAINT pk_concessao_verificacao PRIMARY KEY (tenant_id, concessao_id),
        CONSTRAINT fk_concessao_verificacao_inventario
            FOREIGN KEY (tenant_id, inventario_id, empresa_id)
            REFERENCES inventario (tenant_id, inventario_id, empresa_id),
        CONSTRAINT ck_concessao_verificacao_email CHECK (email = lower(email) AND email ~ '@'),
        CONSTRAINT ck_concessao_verificacao_segredo CHECK (length(segredo_hash) = 32),
        CONSTRAINT ck_concessao_verificacao_prazo CHECK (expira_em > criado_em),
        CONSTRAINT ck_concessao_verificacao_aceite CHECK ((aceito_em IS NULL) = (usuario_id IS NULL)),
        CONSTRAINT ck_concessao_verificacao_revogacao CHECK (
            (revogado_em IS NULL) = (revogado_por IS NULL)
        )
    )
    """,
    "CREATE INDEX ix_concessao_verificacao_tenant_id_inventario_id"
    " ON concessao_verificacao (tenant_id, inventario_id)",
    """
    CREATE FUNCTION contexto_concessao() RETURNS uuid LANGUAGE sql STABLE AS $$
        SELECT nullif(current_setting('app.concessao_id', true), '')::uuid
    $$
    """,
    # Inventario e empresa da concessao do contexto, so se ela vale agora para esta pessoa.
    """
    CREATE FUNCTION verificacao_inventario() RETURNS uuid LANGUAGE sql STABLE AS $$
        SELECT c.inventario_id FROM concessao_verificacao c
         WHERE c.tenant_id = contexto_tenant_id()
           AND c.concessao_id = contexto_concessao()
           AND c.usuario_id = contexto_usuario_id()
           AND c.aceito_em IS NOT NULL AND c.revogado_em IS NULL AND c.expira_em > now()
    $$
    """,
    """
    CREATE FUNCTION verificacao_empresa() RETURNS uuid LANGUAGE sql STABLE AS $$
        SELECT c.empresa_id FROM concessao_verificacao c
         WHERE c.tenant_id = contexto_tenant_id()
           AND c.concessao_id = contexto_concessao()
           AND c.usuario_id = contexto_usuario_id()
           AND c.aceito_em IS NOT NULL AND c.revogado_em IS NULL AND c.expira_em > now()
    $$
    """,
    f"GRANT EXECUTE ON FUNCTION contexto_concessao(), verificacao_inventario(),"
    f" verificacao_empresa() TO {PAPEL_APP}, {PAPEL_VERIFICADOR}",
    f"GRANT SELECT ON {', '.join(REFERENCIA)} TO {PAPEL_VERIFICADOR}",
    f"GRANT SELECT, UPDATE ON concessao_verificacao TO {PAPEL_IDENTIDADE}",
    f"GRANT SELECT ON inventario TO {PAPEL_IDENTIDADE}",
]

FUNCOES = {
    "identidade_aceitar_concessao(uuid, uuid, bytea, text, text, text, text)": f"""
CREATE FUNCTION identidade_aceitar_concessao(
    p_tenant_id uuid, p_concessao_id uuid, p_segredo_hash bytea,
    p_provedor text, p_sujeito text, p_email text, p_nome text
) RETURNS uuid LANGUAGE plpgsql VOLATILE {DEFINIDOR} AS $$
DECLARE
    v_concessao concessao_verificacao%ROWTYPE;
    v_usuario uuid;
BEGIN
    SELECT * INTO v_concessao FROM concessao_verificacao
    WHERE tenant_id = p_tenant_id AND concessao_id = p_concessao_id
    FOR UPDATE;
    -- Um unico erro para todos os motivos: quem tenta nao descobre qual falhou.
    IF NOT FOUND
        OR v_concessao.segredo_hash <> p_segredo_hash
        OR v_concessao.aceito_em IS NOT NULL
        OR v_concessao.revogado_em IS NOT NULL
        OR v_concessao.expira_em <= now()
        OR v_concessao.email <> lower(p_email)
    THEN
        RAISE EXCEPTION 'concessao invalida' USING ERRCODE = 'CL001';
    END IF;

    SELECT usuario_id INTO v_usuario FROM identidade
    WHERE provedor = p_provedor AND sujeito = p_sujeito;
    IF v_usuario IS NULL THEN
        SELECT usuario_id INTO v_usuario FROM usuario WHERE email = lower(p_email);
        IF v_usuario IS NULL THEN
            v_usuario := gen_random_uuid();
            INSERT INTO usuario (usuario_id, email, nome)
            VALUES (v_usuario, lower(p_email), p_nome);
        END IF;
        INSERT INTO identidade (provedor, sujeito, usuario_id)
        VALUES (p_provedor, p_sujeito, v_usuario);
    END IF;

    UPDATE concessao_verificacao SET aceito_em = now(), usuario_id = v_usuario
    WHERE tenant_id = p_tenant_id AND concessao_id = p_concessao_id;
    RETURN v_usuario;
END
$$""",
    "identidade_concessoes(uuid)": f"""
CREATE FUNCTION identidade_concessoes(p_usuario_id uuid)
RETURNS TABLE (
    tenant_id uuid, concessao_id uuid, empresa_id uuid, inventario_id uuid,
    tenant_nome text, empresa_nome text, ano smallint, expira_em timestamptz
)
LANGUAGE sql STABLE {DEFINIDOR} AS $$
    SELECT c.tenant_id, c.concessao_id, c.empresa_id, c.inventario_id,
           t.nome, e.nome, i.ano, c.expira_em
    FROM concessao_verificacao c
    JOIN tenant t ON t.tenant_id = c.tenant_id
    JOIN empresa e ON e.tenant_id = c.tenant_id AND e.empresa_id = c.empresa_id
    JOIN inventario i ON i.tenant_id = c.tenant_id AND i.inventario_id = c.inventario_id
    WHERE c.usuario_id = p_usuario_id
      AND c.aceito_em IS NOT NULL AND c.revogado_em IS NULL AND c.expira_em > now()
    ORDER BY t.nome, e.nome, i.ano DESC, c.concessao_id
$$""",
}


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for comando in sql_rls_tabela("concessao_verificacao", por_empresa=True):
        op.execute(comando)
    for comando in sql_conceder("concessao_verificacao", ["SELECT", "INSERT", "UPDATE"]):
        op.execute(comando)
    for tabela, visao in VISAO_DO_VERIFICADOR.items():
        op.execute(
            f"CREATE POLICY {tabela}_verificador ON {tabela} AS RESTRICTIVE FOR SELECT"
            f" TO {PAPEL_VERIFICADOR} USING ({visao})"
        )
    for assinatura, criacao in FUNCOES.items():
        op.execute(criacao)
        op.execute(f"REVOKE ALL ON FUNCTION {assinatura} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {assinatura} TO {PAPEL_APP}")
        op.execute(f"ALTER FUNCTION {assinatura} OWNER TO {PAPEL_IDENTIDADE}")


def downgrade() -> None:
    op.execute(f"SET LOCAL ROLE {PAPEL_IDENTIDADE}")
    for assinatura in FUNCOES:
        op.execute(f"DROP FUNCTION {assinatura}")
    op.execute("RESET ROLE")
    for tabela in VISAO_DO_VERIFICADOR:
        if tabela != "concessao_verificacao":
            op.execute(f"DROP POLICY {tabela}_verificador ON {tabela}")
    op.execute(f"REVOKE SELECT ON {', '.join(REFERENCIA)} FROM {PAPEL_VERIFICADOR}")
    op.execute(f"REVOKE ALL ON inventario FROM {PAPEL_IDENTIDADE}")
    op.execute("DROP TABLE concessao_verificacao")
    op.execute("DROP FUNCTION verificacao_empresa()")
    op.execute("DROP FUNCTION verificacao_inventario()")
    op.execute("DROP FUNCTION contexto_concessao()")
