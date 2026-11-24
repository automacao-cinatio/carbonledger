"""Identidade: usuario, identidade, membro, membro_empresa, convite e funcoes de login.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20

usuario e identidade sao globais: a pessoa existe antes de escolher um tenant. O login le
acessos antes de haver contexto, so pelas funcoes identidade_*, que sao SECURITY DEFINER e
pertencem ao app_identidade (unico papel com BYPASSRLS). O app_user so tem EXECUTE nelas.
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import PAPEL_APP, PAPEL_VERIFICADOR, sql_conceder, sql_rls_tabela

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAPEL_IDENTIDADE = "app_identidade"
PERFIS = "('administrador', 'gestor', 'responsavel', 'colaborador')"

TABELAS = """
CREATE TABLE usuario (
    usuario_id uuid NOT NULL,
    email text NOT NULL,
    nome text NOT NULL,
    criado_em timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_usuario PRIMARY KEY (usuario_id),
    CONSTRAINT uq_usuario_email UNIQUE (email),
    CONSTRAINT ck_usuario_email_minusculo CHECK (email = lower(email))
);

CREATE TABLE identidade (
    provedor text NOT NULL,
    sujeito text NOT NULL,
    usuario_id uuid NOT NULL,
    criado_em timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_identidade PRIMARY KEY (provedor, sujeito),
    CONSTRAINT fk_identidade_usuario_id_usuario FOREIGN KEY (usuario_id)
        REFERENCES usuario (usuario_id)
);

CREATE TABLE membro (
    tenant_id uuid NOT NULL,
    usuario_id uuid NOT NULL,
    perfil text NOT NULL,
    ativo boolean NOT NULL DEFAULT true,
    criado_em timestamptz NOT NULL DEFAULT now(),
    atualizado_em timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_membro PRIMARY KEY (tenant_id, usuario_id),
    CONSTRAINT fk_membro_tenant_id_tenant FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
    CONSTRAINT fk_membro_usuario_id_usuario FOREIGN KEY (usuario_id)
        REFERENCES usuario (usuario_id),
    CONSTRAINT ck_membro_perfil CHECK (perfil IN {perfis})
);

CREATE TABLE membro_empresa (
    tenant_id uuid NOT NULL,
    usuario_id uuid NOT NULL,
    empresa_id uuid NOT NULL,
    criado_em timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_membro_empresa PRIMARY KEY (tenant_id, usuario_id, empresa_id),
    CONSTRAINT fk_membro_empresa_membro FOREIGN KEY (tenant_id, usuario_id)
        REFERENCES membro (tenant_id, usuario_id),
    CONSTRAINT fk_membro_empresa_empresa FOREIGN KEY (tenant_id, empresa_id)
        REFERENCES empresa (tenant_id, empresa_id)
);

CREATE TABLE convite (
    tenant_id uuid NOT NULL,
    convite_id uuid NOT NULL,
    email text NOT NULL,
    perfil text NOT NULL,
    empresas uuid[] NOT NULL DEFAULT '{{}}',
    segredo_hash bytea NOT NULL,
    criado_por uuid,
    criado_em timestamptz NOT NULL DEFAULT now(),
    expira_em timestamptz NOT NULL,
    aceito_em timestamptz,
    aceito_por uuid,
    revogado_em timestamptz,
    CONSTRAINT pk_convite PRIMARY KEY (tenant_id, convite_id),
    CONSTRAINT fk_convite_tenant_id_tenant FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
    CONSTRAINT fk_convite_criado_por_usuario FOREIGN KEY (criado_por)
        REFERENCES usuario (usuario_id),
    CONSTRAINT fk_convite_aceito_por_usuario FOREIGN KEY (aceito_por)
        REFERENCES usuario (usuario_id),
    CONSTRAINT ck_convite_email_minusculo CHECK (email = lower(email)),
    CONSTRAINT ck_convite_perfil CHECK (perfil IN {perfis}),
    CONSTRAINT ck_convite_segredo_hash CHECK (octet_length(segredo_hash) = 32),
    -- Administrador e gestor veem o tenant inteiro; responsavel e colaborador, so empresas.
    CONSTRAINT ck_convite_empresas_por_perfil CHECK (
        (perfil IN ('administrador', 'gestor') AND cardinality(empresas) = 0)
        OR (perfil IN ('responsavel', 'colaborador') AND cardinality(empresas) > 0)
    )
);
""".format(perfis=PERFIS)

# usuario: a pessoa ve a si mesma; com contexto de tenant, ve quem e membro dele (a
# subconsulta em membro passa pelo RLS de membro). Sem contexto, nada.
RLS_GLOBAL = f"""
ALTER TABLE usuario ENABLE ROW LEVEL SECURITY;
ALTER TABLE usuario FORCE ROW LEVEL SECURITY;
CREATE POLICY usuario_proprio ON usuario AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (usuario_id = (SELECT contexto_usuario_id()));
CREATE POLICY usuario_do_tenant ON usuario AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (EXISTS (SELECT 1 FROM membro m WHERE m.usuario_id = usuario.usuario_id));
REVOKE ALL ON usuario FROM PUBLIC;
GRANT SELECT ON usuario TO {PAPEL_APP}, {PAPEL_VERIFICADOR};

-- identidade: ninguem da aplicacao le direto; so as funcoes de login.
ALTER TABLE identidade ENABLE ROW LEVEL SECURITY;
ALTER TABLE identidade FORCE ROW LEVEL SECURITY;
REVOKE ALL ON identidade FROM PUBLIC;
"""

PRIVILEGIOS_IDENTIDADE = f"""
GRANT SELECT ON tenant, empresa, usuario, identidade, membro, membro_empresa, convite
    TO {PAPEL_IDENTIDADE};
GRANT INSERT ON usuario, identidade, membro, membro_empresa TO {PAPEL_IDENTIDADE};
GRANT UPDATE ON membro, convite TO {PAPEL_IDENTIDADE};
GRANT DELETE ON membro_empresa TO {PAPEL_IDENTIDADE};
"""

DEFINIDOR = "SECURITY DEFINER SET search_path = pg_catalog, public"

FUNCOES = {
    "identidade_localizar(text, text)": f"""
CREATE FUNCTION identidade_localizar(p_provedor text, p_sujeito text)
RETURNS uuid LANGUAGE sql STABLE {DEFINIDOR} AS $$
    SELECT usuario_id FROM identidade WHERE provedor = p_provedor AND sujeito = p_sujeito
$$""",
    "identidade_usuario(uuid)": f"""
CREATE FUNCTION identidade_usuario(p_usuario_id uuid)
RETURNS TABLE (email text, nome text) LANGUAGE sql STABLE {DEFINIDOR} AS $$
    SELECT u.email, u.nome FROM usuario u WHERE u.usuario_id = p_usuario_id
$$""",
    "identidade_acessos(uuid)": f"""
CREATE FUNCTION identidade_acessos(p_usuario_id uuid)
RETURNS TABLE (tenant_id uuid, tenant_nome text, perfil text)
LANGUAGE sql STABLE {DEFINIDOR} AS $$
    SELECT m.tenant_id, t.nome, m.perfil
    FROM membro m JOIN tenant t ON t.tenant_id = m.tenant_id
    WHERE m.usuario_id = p_usuario_id AND m.ativo
    ORDER BY t.nome, m.tenant_id
$$""",
    "identidade_acesso(uuid, uuid)": f"""
CREATE FUNCTION identidade_acesso(p_usuario_id uuid, p_tenant_id uuid)
RETURNS TABLE (perfil text, empresas uuid[]) LANGUAGE sql STABLE {DEFINIDOR} AS $$
    SELECT m.perfil,
           coalesce(array_agg(me.empresa_id ORDER BY me.empresa_id)
                    FILTER (WHERE me.empresa_id IS NOT NULL), '{{}}')
    FROM membro m
    LEFT JOIN membro_empresa me
        ON me.tenant_id = m.tenant_id AND me.usuario_id = m.usuario_id
    WHERE m.tenant_id = p_tenant_id AND m.usuario_id = p_usuario_id AND m.ativo
    GROUP BY m.perfil
$$""",
    "identidade_aceitar_convite(uuid, uuid, bytea, text, text, text, text)": f"""
CREATE FUNCTION identidade_aceitar_convite(
    p_tenant_id uuid, p_convite_id uuid, p_segredo_hash bytea,
    p_provedor text, p_sujeito text, p_email text, p_nome text
) RETURNS uuid LANGUAGE plpgsql VOLATILE {DEFINIDOR} AS $$
DECLARE
    v_convite convite%ROWTYPE;
    v_usuario uuid;
BEGIN
    SELECT * INTO v_convite FROM convite
    WHERE tenant_id = p_tenant_id AND convite_id = p_convite_id
    FOR UPDATE;
    -- Um unico erro para todos os motivos: quem tenta nao descobre qual falhou.
    IF NOT FOUND
        OR v_convite.segredo_hash <> p_segredo_hash
        OR v_convite.aceito_em IS NOT NULL
        OR v_convite.revogado_em IS NOT NULL
        OR v_convite.expira_em <= now()
        OR v_convite.email <> lower(p_email)
    THEN
        RAISE EXCEPTION 'convite invalido' USING ERRCODE = 'CL001';
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

    INSERT INTO membro (tenant_id, usuario_id, perfil)
    VALUES (p_tenant_id, v_usuario, v_convite.perfil)
    ON CONFLICT (tenant_id, usuario_id)
    DO UPDATE SET perfil = EXCLUDED.perfil, ativo = true, atualizado_em = now();

    DELETE FROM membro_empresa WHERE tenant_id = p_tenant_id AND usuario_id = v_usuario;
    INSERT INTO membro_empresa (tenant_id, usuario_id, empresa_id)
    SELECT p_tenant_id, v_usuario, e FROM unnest(v_convite.empresas) AS e;

    UPDATE convite SET aceito_em = now(), aceito_por = v_usuario
    WHERE tenant_id = p_tenant_id AND convite_id = p_convite_id;
    RETURN v_usuario;
END
$$""",
}


def _executar_bloco(bloco: str) -> None:
    """O asyncpg prepara cada comando: um bloco com varios vira varios execute()."""
    for comando in bloco.split(";\n"):
        linhas = [linha for linha in comando.strip().splitlines() if not linha.startswith("--")]
        if linhas:
            op.execute("\n".join(linhas))


def upgrade() -> None:
    _executar_bloco(TABELAS)
    _executar_bloco(RLS_GLOBAL)

    for comando in sql_rls_tabela("membro", por_empresa=False):
        op.execute(comando)
    for comando in sql_conceder("membro", ["SELECT", "UPDATE"]):
        op.execute(comando)

    for comando in sql_rls_tabela("membro_empresa", por_empresa=True):
        op.execute(comando)
    for comando in sql_conceder("membro_empresa", ["SELECT", "INSERT", "DELETE"]):
        op.execute(comando)

    for comando in sql_rls_tabela("convite", por_empresa=False):
        op.execute(comando)
    for comando in sql_conceder("convite", ["SELECT", "INSERT", "UPDATE"]):
        op.execute(comando)

    _executar_bloco(PRIVILEGIOS_IDENTIDADE)
    for assinatura, criacao in FUNCOES.items():
        op.execute(criacao)
        # Permissoes antes de passar a posse: depois disso o app_owner nao revoga mais nada
        # (o Postgres so avisa) e o EXECUTE padrao para PUBLIC ficaria valendo.
        op.execute(f"REVOKE ALL ON FUNCTION {assinatura} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {assinatura} TO {PAPEL_APP}")
        op.execute(f"ALTER FUNCTION {assinatura} OWNER TO {PAPEL_IDENTIDADE}")


def downgrade() -> None:
    # So o dono apaga a funcao; o app_owner assume o app_identidade so para isso.
    op.execute(f"SET LOCAL ROLE {PAPEL_IDENTIDADE}")
    for assinatura in FUNCOES:
        op.execute(f"DROP FUNCTION {assinatura}")
    op.execute("RESET ROLE")
    op.execute(f"REVOKE ALL ON tenant, empresa FROM {PAPEL_IDENTIDADE}")
    op.execute("DROP TABLE convite, membro_empresa, membro, identidade, usuario")
