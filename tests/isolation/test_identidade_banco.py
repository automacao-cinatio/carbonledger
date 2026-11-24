"""Identidade no banco: o papel app_identidade, as funcoes de login e o RLS de usuario."""

import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from tests.isolation.conftest import Banco, Dados
from tests.isolation.tabelas import FUNCOES_IDENTIDADE, GLOBAIS_COM_RLS

pytestmark = pytest.mark.integration

NEGADO = asyncpg.exceptions.InsufficientPrivilegeError
CONVITE_INVALIDO = "CL001"


# ---------------------------------------------------------------- o papel com BYPASSRLS


@pytest.mark.parametrize("tabela", sorted(GLOBAIS_COM_RLS))
async def test_globais_tem_rls_com_force(conexao_admin: asyncpg.Connection, tabela: str) -> None:
    linha = await conexao_admin.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
        tabela,
    )
    assert linha is not None
    assert linha["relrowsecurity"] and linha["relforcerowsecurity"]


async def test_app_identidade_fica_restrito(conexao_admin: asyncpg.Connection) -> None:
    papel = await conexao_admin.fetchrow(
        "SELECT rolbypassrls, rolcanlogin, rolsuper FROM pg_roles WHERE rolname = 'app_identidade'"
    )
    assert papel is not None
    assert papel["rolbypassrls"] and not papel["rolcanlogin"] and not papel["rolsuper"]

    membros = await conexao_admin.fetch(
        "SELECT pg_get_userbyid(member) AS membro, inherit_option, set_option"
        " FROM pg_auth_members WHERE roleid = 'app_identidade'::regrole"
    )
    assert [(m["membro"], m["inherit_option"], m["set_option"]) for m in membros] == [
        ("app_owner", False, True)
    ]

    tabelas = await conexao_admin.fetchval(
        "SELECT count(*) FROM pg_class WHERE relowner = 'app_identidade'::regrole"
    )
    assert tabelas == 0

    funcoes = await conexao_admin.fetch(
        "SELECT proname, prosecdef, proconfig, oid FROM pg_proc"
        " WHERE proowner = 'app_identidade'::regrole"
    )
    assert {f["proname"] for f in funcoes} == FUNCOES_IDENTIDADE
    for funcao in funcoes:
        assert funcao["prosecdef"], funcao["proname"]
        assert "search_path=pg_catalog, public" in (funcao["proconfig"] or []), funcao["proname"]
        for papel_teste, esperado in (
            ("app_user", True),
            ("app_verificador", False),
            ("app_owner", False),
        ):
            tem = await conexao_admin.fetchval(
                "SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", papel_teste, funcao["oid"]
            )
            assert tem is esperado, f"{papel_teste} EXECUTE {funcao['proname']}"


async def test_app_user_nao_le_identidade_nem_assume_o_papel(
    conexao_app: asyncpg.Connection,
) -> None:
    with pytest.raises(NEGADO):
        await conexao_app.fetch("SELECT * FROM identidade")
    with pytest.raises(NEGADO):
        await conexao_app.execute("SET ROLE app_identidade")


# ---------------------------------------------------------------- RLS de usuario


async def _usuarios_visiveis(conexao: asyncpg.Connection, contexto: ContextoTenant) -> set[UUID]:
    async with conexao.transaction():
        await aplicar_contexto(conexao, contexto)
        return {
            linha["usuario_id"] for linha in await conexao.fetch("SELECT usuario_id FROM usuario")
        }


async def test_usuario_sem_contexto_nao_aparece(conexao_app: asyncpg.Connection) -> None:
    assert await conexao_app.fetch("SELECT * FROM usuario") == []


async def test_usuario_ve_os_membros_do_proprio_tenant(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    visiveis = await _usuarios_visiveis(
        conexao_app, ContextoTenant(tenant_id=dados.tenant_a, escopo=Escopo.TENANT)
    )
    assert visiveis == {dados.admin_a, dados.responsavel_a1}


async def test_usuario_se_ve_mesmo_sem_ser_membro_do_tenant(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    visiveis = await _usuarios_visiveis(
        conexao_app,
        ContextoTenant(
            tenant_id=dados.tenant_b,
            escopo=Escopo.EMPRESAS,
            empresas=frozenset(),
            usuario_id=dados.admin_a,
        ),
    )
    # Ve a si mesmo (usuario_proprio) e os membros de B (usuario_do_tenant).
    assert visiveis == {dados.admin_a, dados.admin_b, dados.responsavel_b1}


# ---------------------------------------------------------------- funcoes de login


async def _criar_convite(
    banco: Banco,
    tenant_id: UUID,
    email: str,
    perfil: str,
    empresas: list[UUID],
    *,
    expira: timedelta = timedelta(days=7),
) -> tuple[UUID, bytes]:
    segredo = uuid4().bytes * 2
    convite_id = uuid4()
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        await admin.execute(
            "INSERT INTO convite (tenant_id, convite_id, email, perfil, empresas, segredo_hash,"
            " expira_em) VALUES ($1, $2, $3, $4, $5, $6, now() + $7)",
            tenant_id,
            convite_id,
            email,
            perfil,
            empresas,
            hashlib.sha256(segredo).digest(),
            expira,
        )
    finally:
        await admin.close()
    return convite_id, segredo


async def _aceitar(
    conexao: asyncpg.Connection,
    tenant_id: UUID,
    convite_id: UUID,
    segredo: bytes,
    *,
    sujeito: str,
    email: str,
    provedor: str = "dev",
) -> UUID:
    async with conexao.transaction():
        return await conexao.fetchval(
            "SELECT identidade_aceitar_convite($1, $2, $3, $4, $5, $6, 'Pessoa')",
            tenant_id,
            convite_id,
            hashlib.sha256(segredo).digest(),
            provedor,
            sujeito,
            email,
        )


async def _limpar_usuario(banco: Banco, email: str) -> None:
    admin = await asyncpg.connect(banco.dsn_admin)
    try:
        usuario = await admin.fetchval("SELECT usuario_id FROM usuario WHERE email = $1", email)
        await admin.execute("DELETE FROM convite WHERE email = $1", email)
        if usuario is not None:
            await admin.execute("DELETE FROM membro_empresa WHERE usuario_id = $1", usuario)
            await admin.execute("DELETE FROM membro WHERE usuario_id = $1", usuario)
            await admin.execute("DELETE FROM identidade WHERE usuario_id = $1", usuario)
            await admin.execute("DELETE FROM usuario WHERE usuario_id = $1", usuario)
    finally:
        await admin.close()


async def test_aceitar_convite_cria_usuario_membro_e_empresas(
    conexao_app: asyncpg.Connection, banco: Banco, dados: Dados
) -> None:
    email = "nova@a.example"
    try:
        convite_id, segredo = await _criar_convite(
            banco, dados.tenant_a, email, "colaborador", [dados.empresa_a2]
        )
        usuario = await _aceitar(
            conexao_app,
            dados.tenant_a,
            convite_id,
            segredo,
            sujeito="s-nova",
            email="Nova@A.example",
        )
        assert await conexao_app.fetchval("SELECT identidade_localizar('dev', 's-nova')") == usuario
        acesso = await conexao_app.fetchrow(
            "SELECT * FROM identidade_acesso($1, $2)", usuario, dados.tenant_a
        )
        assert acesso is not None
        assert acesso["perfil"] == "colaborador"
        assert list(acesso["empresas"]) == [dados.empresa_a2]
        assert (
            await conexao_app.fetchrow(
                "SELECT * FROM identidade_acesso($1, $2)", usuario, dados.tenant_b
            )
            is None
        )
        acessos = await conexao_app.fetch("SELECT * FROM identidade_acessos($1)", usuario)
        assert [(a["tenant_id"], a["perfil"]) for a in acessos] == [(dados.tenant_a, "colaborador")]

        # Convite usado nao serve de novo.
        with pytest.raises(asyncpg.PostgresError) as erro:
            await _aceitar(
                conexao_app, dados.tenant_a, convite_id, segredo, sujeito="s-nova", email=email
            )
        assert erro.value.sqlstate == CONVITE_INVALIDO
    finally:
        await _limpar_usuario(banco, email)


@pytest.mark.parametrize("motivo", ["segredo", "email", "expirado", "revogado", "outro_tenant"])
async def test_convite_invalido_nao_cria_nada(
    conexao_app: asyncpg.Connection, banco: Banco, dados: Dados, motivo: str
) -> None:
    email = f"{motivo}@a.example"
    try:
        convite_id, segredo = await _criar_convite(
            banco,
            dados.tenant_a,
            email,
            "gestor",
            [],
            expira=timedelta(minutes=-1 if motivo == "expirado" else 10_080),
        )
        if motivo == "revogado":
            admin = await asyncpg.connect(banco.dsn_admin)
            await admin.execute(
                "UPDATE convite SET revogado_em = now() WHERE convite_id = $1", convite_id
            )
            await admin.close()
        with pytest.raises(asyncpg.PostgresError) as erro:
            await _aceitar(
                conexao_app,
                dados.tenant_b if motivo == "outro_tenant" else dados.tenant_a,
                convite_id,
                b"x" * 32 if motivo == "segredo" else segredo,
                sujeito=f"s-{motivo}",
                email="outra@a.example" if motivo == "email" else email,
            )
        assert erro.value.sqlstate == CONVITE_INVALIDO
        assert (
            await conexao_app.fetchval("SELECT identidade_localizar('dev', $1)", f"s-{motivo}")
            is None
        )
    finally:
        await _limpar_usuario(banco, email)


async def test_convite_com_empresa_de_outro_tenant_falha(
    conexao_app: asyncpg.Connection, banco: Banco, dados: Dados
) -> None:
    email = "cruzado@a.example"
    try:
        convite_id, segredo = await _criar_convite(
            banco, dados.tenant_a, email, "responsavel", [dados.empresa_b1]
        )
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            await _aceitar(
                conexao_app, dados.tenant_a, convite_id, segredo, sujeito="s-x", email=email
            )
        assert await conexao_app.fetchval("SELECT identidade_localizar('dev', 's-x')") is None
    finally:
        await _limpar_usuario(banco, email)


async def test_mesmo_usuario_em_dois_tenants_e_dois_provedores(
    conexao_app: asyncpg.Connection, banco: Banco, dados: Dados
) -> None:
    email = "consultora@cinatio.example"
    try:
        c1, s1 = await _criar_convite(banco, dados.tenant_a, email, "gestor", [])
        c2, s2 = await _criar_convite(banco, dados.tenant_b, email, "gestor", [])
        u1 = await _aceitar(conexao_app, dados.tenant_a, c1, s1, sujeito="g-1", email=email)
        u2 = await _aceitar(
            conexao_app, dados.tenant_b, c2, s2, sujeito="m-1", email=email, provedor="microsoft"
        )
        assert u1 == u2
        acessos = await conexao_app.fetch("SELECT tenant_id FROM identidade_acessos($1)", u1)
        assert {a["tenant_id"] for a in acessos} == {dados.tenant_a, dados.tenant_b}
    finally:
        await _limpar_usuario(banco, email)


async def test_membro_inativo_perde_o_acesso(
    conexao_app: asyncpg.Connection, conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    transacao = conexao_admin.transaction()
    await transacao.start()
    try:
        await conexao_admin.execute(
            "UPDATE membro SET ativo = false WHERE usuario_id = $1", dados.responsavel_a1
        )
        sem_acesso = await conexao_admin.fetchrow(
            "SELECT * FROM identidade_acesso($1, $2)", dados.responsavel_a1, dados.tenant_a
        )
        assert sem_acesso is None
    finally:
        await transacao.rollback()
    assert (
        await conexao_app.fetchrow(
            "SELECT * FROM identidade_acesso($1, $2)", dados.responsavel_a1, dados.tenant_a
        )
        is not None
    )
