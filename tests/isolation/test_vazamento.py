"""Suite de vazamento: o app_user so enxerga e so grava o que o contexto permite.

Os testes genericos rodam para toda tabela de tests/isolation/tabelas.py; os especificos
exercitam escrita cruzada, escopo de empresas, contexto local e o papel do verificador.
"""

from uuid import UUID, uuid4

import asyncpg
import pytest

from app.core.tenancy import ContextoInvalido, ContextoTenant, Escopo, aplicar_contexto
from tests.isolation.conftest import Dados
from tests.isolation.tabelas import TABELAS_COM_TENANT

pytestmark = pytest.mark.integration

TABELAS = sorted(TABELAS_COM_TENANT)
TABELAS_POR_EMPRESA = sorted(t for t, por_empresa in TABELAS_COM_TENANT.items() if por_empresa)

NEGADO = asyncpg.exceptions.InsufficientPrivilegeError


def _contexto_tenant(tenant_id: UUID) -> ContextoTenant:
    return ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)


def _contexto_empresas(tenant_id: UUID, *empresas: UUID) -> ContextoTenant:
    return ContextoTenant(tenant_id=tenant_id, escopo=Escopo.EMPRESAS, empresas=frozenset(empresas))


async def _linhas(conexao: asyncpg.Connection, tabela: str) -> list[asyncpg.Record]:
    return list(await conexao.fetch(f"SELECT * FROM {tabela}"))  # noqa: S608  # nosec B608


# ---------------------------------------------------------------- testes genericos


@pytest.mark.parametrize("tabela", TABELAS)
async def test_sem_contexto_nada_aparece(conexao_app: asyncpg.Connection, tabela: str) -> None:
    assert await _linhas(conexao_app, tabela) == []
    async with conexao_app.transaction():
        assert await _linhas(conexao_app, tabela) == []


@pytest.mark.parametrize("tabela", TABELAS)
async def test_contexto_de_tenant_ve_so_o_proprio_tenant(
    conexao_app: asyncpg.Connection,
    conexao_admin: asyncpg.Connection,
    dados: Dados,
    tabela: str,
) -> None:
    for tenant in (dados.tenant_a, dados.tenant_b):
        esperado = await conexao_admin.fetchval(
            f"SELECT count(*) FROM {tabela} WHERE tenant_id = $1",  # noqa: S608  # nosec B608
            tenant,
        )
        async with conexao_app.transaction():
            await aplicar_contexto(conexao_app, _contexto_tenant(tenant))
            linhas = await _linhas(conexao_app, tabela)
        assert esperado > 0
        assert len(linhas) == esperado
        assert {linha["tenant_id"] for linha in linhas} == {tenant}


@pytest.mark.parametrize("tabela", TABELAS_POR_EMPRESA)
async def test_escopo_de_empresas_ve_so_as_empresas_listadas(
    conexao_app: asyncpg.Connection,
    conexao_admin: asyncpg.Connection,
    dados: Dados,
    tabela: str,
) -> None:
    # O esperado vem dos dados reais (lidos sem RLS): vale para qualquer tabela por empresa.
    existentes = {
        linha["empresa_id"]
        for linha in await conexao_admin.fetch(
            f"SELECT DISTINCT empresa_id FROM {tabela} WHERE tenant_id = $1",  # noqa: S608  # nosec B608
            dados.tenant_a,
        )
    }
    assert dados.empresa_a1 in existentes, "a semente precisa de linha da empresa A1"
    casos = [
        (dados.empresa_a1,),
        (dados.empresa_a1, dados.empresa_a2),
        (),
        # Empresa de outro tenant na lista nao abre nada: a politica de tenant vale antes.
        (dados.empresa_b1,),
        (dados.empresa_a2, dados.empresa_b2),
    ]
    for empresas in casos:
        async with conexao_app.transaction():
            await aplicar_contexto(conexao_app, _contexto_empresas(dados.tenant_a, *empresas))
            linhas = await _linhas(conexao_app, tabela)
        visiveis = {linha["empresa_id"] for linha in linhas}
        assert visiveis == set(empresas) & existentes, empresas


@pytest.mark.parametrize("tabela", TABELAS)
async def test_contexto_nao_sobrevive_a_transacao(
    conexao_app: asyncpg.Connection, dados: Dados, tabela: str
) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))
        assert await _linhas(conexao_app, tabela)
    # Mesma conexao (como num pool): a transacao acabou, o contexto foi junto.
    assert await _linhas(conexao_app, tabela) == []


@pytest.mark.parametrize("tabela", TABELAS)
async def test_verificador_le_no_tenant_e_nao_escreve(
    conexao_app: asyncpg.Connection, dados: Dados, tabela: str
) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))
        await conexao_app.execute("SET LOCAL ROLE app_verificador")
        assert await conexao_app.fetchval("SELECT current_user") == "app_verificador"
        linhas = await _linhas(conexao_app, tabela)
        assert linhas
        assert {linha["tenant_id"] for linha in linhas} == {dados.tenant_a}
        with pytest.raises(NEGADO):
            await conexao_app.execute(
                f"UPDATE {tabela} SET tenant_id = tenant_id"  # noqa: S608  # nosec B608
            )
    assert await conexao_app.fetchval("SELECT current_user") == "app_user"


# ---------------------------------------------------------------- escrita


async def test_insert_sem_contexto_e_recusado(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    async with conexao_app.transaction():
        with pytest.raises(NEGADO):
            await conexao_app.execute(
                "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome)"
                " VALUES ($1, $2, $3, 'X')",
                dados.tenant_a,
                uuid4(),
                dados.grupo_a,
            )


async def test_insert_em_outro_tenant_e_recusado(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))
        with pytest.raises(NEGADO):
            await conexao_app.execute(
                "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome)"
                " VALUES ($1, $2, $3, 'X')",
                dados.tenant_b,
                uuid4(),
                dados.grupo_b,
            )


async def test_insert_no_proprio_tenant_funciona(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))
        nova = uuid4()
        await conexao_app.execute(
            "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome)"
            " VALUES ($1, $2, $3, 'Nova')",
            dados.tenant_a,
            nova,
            dados.grupo_a,
        )
        assert (
            await conexao_app.fetchval("SELECT count(*) FROM empresa WHERE empresa_id = $1", nova)
            == 1
        )
    finally:
        await transacao.rollback()


async def test_insert_fora_das_empresas_do_escopo_e_recusado(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto_empresas(dados.tenant_a, dados.empresa_a1))
        with pytest.raises(NEGADO):
            await conexao_app.execute(
                "INSERT INTO empresa (tenant_id, empresa_id, grupo_id, nome)"
                " VALUES ($1, $2, $3, 'X')",
                dados.tenant_a,
                uuid4(),
                dados.grupo_a,
            )


async def test_update_nao_move_linha_para_outro_tenant(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))
        with pytest.raises(NEGADO):
            await conexao_app.execute(
                "UPDATE empresa SET tenant_id = $1 WHERE empresa_id = $2",
                dados.tenant_b,
                dados.empresa_a1,
            )


async def test_update_em_linha_de_outro_tenant_nao_afeta_nada(
    conexao_app: asyncpg.Connection, conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))
        resultado = await conexao_app.execute(
            "UPDATE empresa SET nome = 'invadida' WHERE empresa_id = $1", dados.empresa_b1
        )
    assert resultado == "UPDATE 0"
    nome = await conexao_admin.fetchval(
        "SELECT nome FROM empresa WHERE empresa_id = $1", dados.empresa_b1
    )
    assert nome == "Empresa B1"


async def test_delete_e_negado(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))
        with pytest.raises(NEGADO):
            await conexao_app.execute("DELETE FROM empresa WHERE empresa_id = $1", dados.empresa_a1)


# ---------------------------------------------------------------- contexto malformado


async def test_escopo_desconhecido_nao_abre_nada(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    async with conexao_app.transaction():
        await conexao_app.execute(
            "SELECT set_config('app.tenant_id', $1, true), set_config('app.escopo', 'todas', true),"
            " set_config('app.empresas', $2, true)",
            str(dados.tenant_a),
            "{" + str(dados.empresa_a1) + "}",
        )
        assert await _linhas(conexao_app, "empresa") == []


async def test_tenant_id_invalido_falha_em_vez_de_vazar(conexao_app: asyncpg.Connection) -> None:
    async with conexao_app.transaction():
        await conexao_app.execute("SELECT set_config('app.tenant_id', 'qualquer', true)")
        with pytest.raises(asyncpg.exceptions.InvalidTextRepresentationError):
            await _linhas(conexao_app, "empresa")


async def test_contexto_fora_de_transacao_e_recusado(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    with pytest.raises(ContextoInvalido):
        await aplicar_contexto(conexao_app, _contexto_tenant(dados.tenant_a))


# ---------------------------------------------------------------- papel


async def test_app_user_nao_desliga_rls_nem_vira_dono(conexao_app: asyncpg.Connection) -> None:
    with pytest.raises(NEGADO):
        await conexao_app.execute("ALTER TABLE empresa DISABLE ROW LEVEL SECURITY")
    with pytest.raises(NEGADO):
        await conexao_app.execute("SET ROLE app_owner")
    with pytest.raises(NEGADO):
        await conexao_app.execute("CREATE TABLE intrusa (id int)")
