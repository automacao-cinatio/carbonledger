"""LGPD: pedidos do titular, anonimizacao e IA ligada por cliente.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23

- tenant.ia_habilitada: a IA (que envia descricoes e numeros a um operador externo) so funciona
  no cliente que a ligou. Desligada por padrao.
- lgpd_pedido: pedidos de exclusao feitos pela propria pessoa. Tabela global (a pessoa existe
  antes e acima dos clientes), sem acesso direto do app_user: so pelas funcoes abaixo.
- Anonimizacao (decisao do usuario): nome e e-mail viram um codigo; os vinculos de login
  (identidade) sao removidos; acessos, convites pendentes e concessoes sao encerrados. A trilha
  e os dados aprovados ficam com o id, que ja nao identifica ninguem: nada some.

Revisao de seguranca das quatro funcoes novas do app_identidade (SECURITY DEFINER, search_path
fixo, EXECUTE so para o app_user):
- identidade_lgpd_pedir(usuario): grava um pedido de exclusao da propria pessoa (a aplicacao
  passa o usuario da sessao); no maximo um pedido em aberto;
- identidade_lgpd_pedidos(usuario): os pedidos da propria pessoa;
- identidade_lgpd_pendentes(): pedidos em aberto (id do pedido, id da pessoa, data), para a
  linha de comando da plataforma; nenhum nome ou e-mail;
- identidade_lgpd_anonimizar(usuario, executor): executa a anonimizacao, uma vez, e devolve os
  tenants em que a pessoa era membro (para a trilha de cada um).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINIDOR = "SECURITY DEFINER SET search_path = pg_catalog, public"
PAPEL_IDENTIDADE = "app_identidade"

COMANDOS = [
    "ALTER TABLE tenant ADD COLUMN ia_habilitada boolean NOT NULL DEFAULT false",
    """
    CREATE TABLE lgpd_pedido (
        pedido_id uuid NOT NULL,
        usuario_id uuid NOT NULL,
        tipo text NOT NULL,
        solicitado_em timestamptz NOT NULL DEFAULT now(),
        executado_em timestamptz,
        executado_por text,
        CONSTRAINT pk_lgpd_pedido PRIMARY KEY (pedido_id),
        CONSTRAINT fk_lgpd_pedido_usuario FOREIGN KEY (usuario_id) REFERENCES usuario (usuario_id),
        CONSTRAINT ck_lgpd_pedido_tipo CHECK (tipo IN ('exclusao')),
        CONSTRAINT ck_lgpd_pedido_execucao CHECK (
            (executado_em IS NULL) = (executado_por IS NULL)
        )
    )
    """,
    "CREATE UNIQUE INDEX uq_lgpd_pedido_usuario_id_aberto ON lgpd_pedido (usuario_id)"
    " WHERE executado_em IS NULL",
    "ALTER TABLE lgpd_pedido ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE lgpd_pedido FORCE ROW LEVEL SECURITY",
    "REVOKE ALL ON lgpd_pedido FROM PUBLIC",
    f"GRANT SELECT, INSERT, UPDATE ON lgpd_pedido TO {PAPEL_IDENTIDADE}",
    f"GRANT UPDATE ON usuario TO {PAPEL_IDENTIDADE}",
    f"GRANT DELETE ON identidade TO {PAPEL_IDENTIDADE}",
]

FUNCOES = {
    "identidade_lgpd_pedir(uuid)": f"""
CREATE FUNCTION identidade_lgpd_pedir(p_usuario_id uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE {DEFINIDOR} AS $$
DECLARE
    v_pedido uuid;
BEGIN
    SELECT pedido_id INTO v_pedido FROM lgpd_pedido
     WHERE usuario_id = p_usuario_id AND executado_em IS NULL;
    IF v_pedido IS NULL THEN
        v_pedido := gen_random_uuid();
        INSERT INTO lgpd_pedido (pedido_id, usuario_id, tipo)
        VALUES (v_pedido, p_usuario_id, 'exclusao');
    END IF;
    RETURN v_pedido;
END
$$""",
    "identidade_lgpd_pedidos(uuid)": f"""
CREATE FUNCTION identidade_lgpd_pedidos(p_usuario_id uuid)
RETURNS TABLE (pedido_id uuid, tipo text, solicitado_em timestamptz, executado_em timestamptz)
LANGUAGE sql STABLE {DEFINIDOR} AS $$
    SELECT pedido_id, tipo, solicitado_em, executado_em FROM lgpd_pedido
     WHERE usuario_id = p_usuario_id ORDER BY solicitado_em DESC
$$""",
    "identidade_lgpd_pendentes()": f"""
CREATE FUNCTION identidade_lgpd_pendentes()
RETURNS TABLE (pedido_id uuid, usuario_id uuid, solicitado_em timestamptz)
LANGUAGE sql STABLE {DEFINIDOR} AS $$
    SELECT pedido_id, usuario_id, solicitado_em FROM lgpd_pedido
     WHERE executado_em IS NULL ORDER BY solicitado_em
$$""",
    "identidade_lgpd_anonimizar(uuid, text)": f"""
CREATE FUNCTION identidade_lgpd_anonimizar(p_usuario_id uuid, p_executor text)
RETURNS SETOF uuid LANGUAGE plpgsql VOLATILE {DEFINIDOR} AS $$
DECLARE
    v_email text;
    v_codigo text := 'anonimizado-' || p_usuario_id::text || '@anonimizado.invalid';
BEGIN
    IF length(btrim(coalesce(p_executor, ''))) = 0 THEN
        RAISE EXCEPTION 'informe quem executa' USING ERRCODE = 'CL030';
    END IF;
    SELECT email INTO v_email FROM usuario WHERE usuario_id = p_usuario_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pessoa inexistente' USING ERRCODE = 'CL030';
    END IF;
    IF v_email = v_codigo THEN
        RAISE EXCEPTION 'pessoa ja anonimizada' USING ERRCODE = 'CL031';
    END IF;

    RETURN QUERY SELECT DISTINCT m.tenant_id FROM membro m WHERE m.usuario_id = p_usuario_id;

    UPDATE usuario SET nome = 'Pessoa anonimizada', email = v_codigo
     WHERE usuario_id = p_usuario_id;
    DELETE FROM identidade WHERE usuario_id = p_usuario_id;
    UPDATE membro SET ativo = false, atualizado_em = now() WHERE usuario_id = p_usuario_id;
    DELETE FROM membro_empresa WHERE usuario_id = p_usuario_id;
    UPDATE convite SET email = v_codigo, revogado_em = coalesce(revogado_em, now())
     WHERE email = v_email;
    UPDATE concessao_verificacao
       SET email = v_codigo,
           revogado_em = coalesce(revogado_em, now()),
           revogado_por = coalesce(revogado_por, p_usuario_id)
     WHERE email = v_email OR usuario_id = p_usuario_id;
    UPDATE lgpd_pedido SET executado_em = now(), executado_por = btrim(p_executor)
     WHERE usuario_id = p_usuario_id AND executado_em IS NULL;
END
$$""",
}


def upgrade() -> None:
    for comando in COMANDOS:
        op.execute(comando)
    for assinatura, criacao in FUNCOES.items():
        op.execute(criacao)
        op.execute(f"REVOKE ALL ON FUNCTION {assinatura} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {assinatura} TO app_user")
        op.execute(f"ALTER FUNCTION {assinatura} OWNER TO {PAPEL_IDENTIDADE}")


def downgrade() -> None:
    op.execute(f"SET LOCAL ROLE {PAPEL_IDENTIDADE}")
    for assinatura in FUNCOES:
        op.execute(f"DROP FUNCTION {assinatura}")
    op.execute("RESET ROLE")
    op.execute(f"REVOKE DELETE ON identidade FROM {PAPEL_IDENTIDADE}")
    op.execute(f"REVOKE UPDATE ON usuario FROM {PAPEL_IDENTIDADE}")
    op.execute("DROP TABLE lgpd_pedido")
    op.execute("ALTER TABLE tenant DROP COLUMN ia_habilitada")
