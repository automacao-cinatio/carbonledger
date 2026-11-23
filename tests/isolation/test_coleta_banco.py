"""Coleta no banco: so insercao, segregacao de funcoes, periodo e chaves compostas.

As regras valem para qualquer caminho de escrita (API, worker, linha de comando), porque
moram nas triggers e nas restricoes da migracao 0006.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from tests.isolation.conftest import Dados
from tests.isolation.tabelas import TABELAS_SO_INSERCAO

pytestmark = pytest.mark.integration

NEGADO = asyncpg.exceptions.InsufficientPrivilegeError


def _contexto(dados: Dados, usuario: UUID | None = None) -> ContextoTenant:
    return ContextoTenant(tenant_id=dados.tenant_a, escopo=Escopo.TENANT, usuario_id=usuario)


@pytest.fixture
async def transacao_a(
    conexao_app: asyncpg.Connection, dados: Dados
) -> AsyncIterator[asyncpg.Connection]:
    """Transacao do tenant A que sempre volta atras: os testes nao sujam a semente."""
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(conexao_app, _contexto(dados))
        yield conexao_app
    finally:
        await transacao.rollback()


async def _sqlstate(conexao: asyncpg.Connection, sql: str, *argumentos: object) -> str:
    with pytest.raises(asyncpg.PostgresError) as erro:
        async with conexao.transaction():
            await conexao.execute(sql, *argumentos)
    return str(erro.value.sqlstate)


async def _lancar(
    conexao: asyncpg.Connection,
    dados: Dados,
    *,
    periodo: int = 2,
    versao: int = 1,
    autor: UUID | None = None,
    fonte: UUID | None = None,
) -> UUID:
    lancamento = uuid4()
    await conexao.execute(
        "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo, versao,"
        " quantidade, unidade, origem, criado_por) VALUES ($1, $2, $3, $4, $5, $6, 10, 'L',"
        " 'manual', $7)",
        dados.tenant_a,
        lancamento,
        dados.empresa_a1,
        fonte or dados.fonte_a1,
        periodo,
        versao,
        autor or dados.responsavel_a1,
    )
    return lancamento


# ---------------------------------------------------------------- so insercao


@pytest.mark.parametrize("tabela", TABELAS_SO_INSERCAO)
async def test_app_user_so_le_e_insere(conexao_admin: asyncpg.Connection, tabela: str) -> None:
    for privilegio, esperado in (
        ("SELECT", True),
        ("INSERT", True),
        ("UPDATE", False),
        ("DELETE", False),
        ("TRUNCATE", False),
    ):
        tem = await conexao_admin.fetchval(
            "SELECT has_table_privilege('app_user', $1::regclass, $2)", tabela, privilegio
        )
        assert tem is esperado, f"app_user {privilegio} em {tabela}"


@pytest.mark.parametrize("tabela", ["inventario", "fonte_emissao"])
async def test_inventario_e_fonte_nao_somem(conexao_admin: asyncpg.Connection, tabela: str) -> None:
    for privilegio in ("DELETE", "TRUNCATE"):
        tem = await conexao_admin.fetchval(
            "SELECT has_table_privilege('app_user', $1::regclass, $2)", tabela, privilegio
        )
        assert tem is False, f"app_user {privilegio} em {tabela}"


@pytest.mark.parametrize("tabela", TABELAS_SO_INSERCAO)
async def test_nem_o_superusuario_altera_ou_apaga(
    conexao_admin: asyncpg.Connection, tabela: str
) -> None:
    for comando in (
        f"UPDATE {tabela} SET tenant_id = tenant_id",  # noqa: S608  # nosec B608
        f"DELETE FROM {tabela}",  # noqa: S608  # nosec B608
        f"TRUNCATE {tabela} CASCADE",
    ):
        with pytest.raises(asyncpg.PostgresError, match="so aceita insercao"):
            async with conexao_admin.transaction():
                await conexao_admin.execute(comando)


# ---------------------------------------------------------------- lancamento


async def test_periodo_segue_a_periodicidade(transacao_a: asyncpg.Connection, dados: Dados) -> None:
    # A fonte da semente e mensal: periodo 0 (ano inteiro) nao vale.
    estado = await _sqlstate(
        transacao_a,
        "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo, versao,"
        " quantidade, unidade, origem) VALUES ($1, gen_random_uuid(), $2, $3, 0, 1, 1, 'L',"
        " 'manual')",
        dados.tenant_a,
        dados.empresa_a1,
        dados.fonte_a1,
    )
    assert estado == "CL013"


async def test_fonte_desativada_nao_recebe_dado(
    transacao_a: asyncpg.Connection, dados: Dados
) -> None:
    await transacao_a.execute(
        "UPDATE fonte_emissao SET ativa = false WHERE fonte_id = $1", dados.fonte_a1
    )
    estado = await _sqlstate(
        transacao_a,
        "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo, versao,"
        " quantidade, unidade, origem) VALUES ($1, gen_random_uuid(), $2, $3, 3, 1, 1, 'L',"
        " 'manual')",
        dados.tenant_a,
        dados.empresa_a1,
        dados.fonte_a1,
    )
    assert estado == "CL012"


async def test_versao_repetida_e_recusada(transacao_a: asyncpg.Connection, dados: Dados) -> None:
    await _lancar(transacao_a, dados, periodo=4)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with transacao_a.transaction():
            await _lancar(transacao_a, dados, periodo=4)


async def test_quantidade_negativa_e_recusada(
    transacao_a: asyncpg.Connection, dados: Dados
) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        async with transacao_a.transaction():
            await transacao_a.execute(
                "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo,"
                " versao, quantidade, unidade, origem) VALUES ($1, gen_random_uuid(), $2, $3, 5,"
                " 1, -1, 'L', 'manual')",
                dados.tenant_a,
                dados.empresa_a1,
                dados.fonte_a1,
            )


# ---------------------------------------------------------------- decisao


async def _decidir(
    conexao: asyncpg.Connection, dados: Dados, lancamento: UUID, quem: UUID, decisao: str
) -> None:
    await conexao.execute(
        "INSERT INTO lancamento_decisao (tenant_id, lancamento_id, empresa_id, decisao, motivo,"
        " decidido_por) VALUES ($1, $2, $3, $4, $5, $6)",
        dados.tenant_a,
        lancamento,
        dados.empresa_a1,
        decisao,
        None if decisao == "aprovado" else "Nao bate com a nota",
        quem,
    )


async def test_quem_lancou_nao_decide(transacao_a: asyncpg.Connection, dados: Dados) -> None:
    lancamento = await _lancar(transacao_a, dados, periodo=6, autor=dados.responsavel_a1)
    with pytest.raises(asyncpg.PostgresError) as erro:
        async with transacao_a.transaction():
            await _decidir(transacao_a, dados, lancamento, dados.responsavel_a1, "aprovado")
    assert erro.value.sqlstate == "CL014"
    await _decidir(transacao_a, dados, lancamento, dados.admin_a, "aprovado")


async def test_so_a_versao_mais_recente_e_decidida(
    transacao_a: asyncpg.Connection, dados: Dados
) -> None:
    antiga = await _lancar(transacao_a, dados, periodo=7, versao=1)
    nova = await _lancar(transacao_a, dados, periodo=7, versao=2)
    with pytest.raises(asyncpg.PostgresError) as erro:
        async with transacao_a.transaction():
            await _decidir(transacao_a, dados, antiga, dados.admin_a, "aprovado")
    assert erro.value.sqlstate == "CL015"
    await _decidir(transacao_a, dados, nova, dados.admin_a, "devolvido")


async def test_decisao_e_gravada_uma_vez(transacao_a: asyncpg.Connection, dados: Dados) -> None:
    lancamento = await _lancar(transacao_a, dados, periodo=8)
    await _decidir(transacao_a, dados, lancamento, dados.admin_a, "aprovado")
    with pytest.raises(asyncpg.UniqueViolationError):
        async with transacao_a.transaction():
            await _decidir(transacao_a, dados, lancamento, dados.admin_a, "devolvido")


async def test_devolver_exige_motivo(transacao_a: asyncpg.Connection, dados: Dados) -> None:
    lancamento = await _lancar(transacao_a, dados, periodo=9)
    with pytest.raises(asyncpg.CheckViolationError):
        async with transacao_a.transaction():
            await transacao_a.execute(
                "INSERT INTO lancamento_decisao (tenant_id, lancamento_id, empresa_id, decisao,"
                " decidido_por) VALUES ($1, $2, $3, 'devolvido', $4)",
                dados.tenant_a,
                lancamento,
                dados.empresa_a1,
                dados.admin_a,
            )


# ---------------------------------------------------------------- chaves e escopo


async def test_fonte_nao_mistura_unidade_de_outra_empresa(
    transacao_a: asyncpg.Connection, dados: Dados
) -> None:
    # Inventario da empresa A1 com unidade que nao e da A1 (finge ser da A2): a chave composta
    # (tenant_id, unidade_id, empresa_id) recusa.
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with transacao_a.transaction():
            await transacao_a.execute(
                "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id,"
                " unidade_id, categoria, descricao, unidade_medida, periodicidade)"
                " VALUES ($1, gen_random_uuid(), $2, $3, $4, 'escopo1.fugitivas', 'X', 'kg',"
                " 'anual')",
                dados.tenant_a,
                dados.empresa_a2,
                dados.inventario_a1,
                dados.unidade_a1,
            )


async def test_fonte_fora_da_materialidade_exige_justificativa(
    transacao_a: asyncpg.Connection, dados: Dados
) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        async with transacao_a.transaction():
            await transacao_a.execute(
                "UPDATE fonte_emissao SET material = false WHERE fonte_id = $1", dados.fonte_a1
            )


async def test_responsavel_nao_lanca_em_empresa_alheia(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    contexto = ContextoTenant(
        tenant_id=dados.tenant_a, escopo=Escopo.EMPRESAS, empresas=frozenset({dados.empresa_a2})
    )
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, contexto)
        # A fonte da A1 nem aparece. A gravacao e barrada antes pela trigger (que nao acha a
        # fonte, CL010); sem ela, o RLS barraria (empresa fora do escopo).
        assert await conexao_app.fetchval("SELECT count(*) FROM fonte_emissao") == 0
        with pytest.raises(asyncpg.PostgresError) as erro:
            await _lancar(conexao_app, dados, periodo=10)
        assert erro.value.sqlstate in ("CL010", "42501")


async def test_inventario_um_por_empresa_e_ano(
    transacao_a: asyncpg.Connection, dados: Dados
) -> None:
    with pytest.raises(asyncpg.UniqueViolationError):
        async with transacao_a.transaction():
            await transacao_a.execute(
                "INSERT INTO inventario (tenant_id, inventario_id, empresa_id, ano, abordagem)"
                " VALUES ($1, gen_random_uuid(), $2, 2025, 'controle_financeiro')",
                dados.tenant_a,
                dados.empresa_a1,
            )
