"""audit_log no banco: encadeamento pela trigger, so insercao, RLS e concorrencia."""

import asyncio
from uuid import UUID

import asyncpg
import pytest

from app.core import audit
from app.core.audit import LinhaTrilha, verificar_cadeia
from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from tests.isolation.conftest import Banco, Dados

pytestmark = pytest.mark.integration

NEGADO = asyncpg.exceptions.InsufficientPrivilegeError
SO_INSERCAO = "CL002"

_SQL_CADEIA = (
    "SELECT tenant_id, seq, evento_id, ocorrido_em, usuario_id, acao, entidade, entidade_id,"
    " dados::text AS dados_texto, hash_anterior, hash"
    " FROM audit_log WHERE tenant_id = $1 ORDER BY seq"
)


async def _cadeia(conexao: asyncpg.Connection, tenant_id: UUID) -> list[LinhaTrilha]:
    return [LinhaTrilha(**dict(linha)) for linha in await conexao.fetch(_SQL_CADEIA, tenant_id)]


def _contexto(tenant_id: UUID) -> ContextoTenant:
    return ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)


async def test_trigger_define_seq_e_hash_e_ignora_o_que_o_app_manda(
    conexao_app: asyncpg.Connection, conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(conexao_app, _contexto(dados.tenant_a))
        ultimo = await conexao_app.fetchval("SELECT max(seq) FROM audit_log")
        await conexao_app.execute(
            "INSERT INTO audit_log (tenant_id, seq, evento_id, ocorrido_em, acao, entidade,"
            " hash_anterior, hash) VALUES ($1, 999, '00000000-0000-4000-8000-000000000000',"
            " '2000-01-01', 'teste.forjado', 'tenant', $2, $2)",
            dados.tenant_a,
            b"\x01" * 32,
        )
        linha = await conexao_app.fetchrow(
            "SELECT seq, evento_id, ocorrido_em, hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        )
        assert linha is not None
        assert linha["seq"] == ultimo + 1
        assert str(linha["evento_id"]) != "00000000-0000-4000-8000-000000000000"
        assert linha["ocorrido_em"].year >= 2026
        assert linha["hash"] != b"\x01" * 32
        cadeia = [
            LinhaTrilha(**dict(r)) for r in await conexao_app.fetch(_SQL_CADEIA, dados.tenant_a)
        ]
        assert verificar_cadeia(cadeia).integra
    finally:
        await transacao.rollback()


async def test_app_user_nao_altera_nem_apaga(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    for comando in (
        "UPDATE audit_log SET acao = 'x.y'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        async with conexao_app.transaction():
            await aplicar_contexto(conexao_app, _contexto(dados.tenant_a))
            with pytest.raises(NEGADO):
                await conexao_app.execute(comando)


@pytest.mark.parametrize(
    "comando",
    ["UPDATE audit_log SET acao = 'x.y'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
async def test_nem_o_superusuario_altera_com_a_trigger_ligada(
    conexao_admin: asyncpg.Connection, comando: str
) -> None:
    async with conexao_admin.transaction():
        with pytest.raises(asyncpg.PostgresError) as erro:
            await conexao_admin.execute(comando)
    assert erro.value.sqlstate == SO_INSERCAO


async def test_cadeias_da_semente_estao_integras(
    conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    for tenant in (dados.tenant_a, dados.tenant_b):
        resultado = verificar_cadeia(await _cadeia(conexao_admin, tenant))
        assert resultado.integra, resultado
        assert resultado.eventos >= 1


async def test_adulteracao_por_fora_e_detectada(
    conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    """Quem desliga a trigger e altera um evento nao passa pela verificacao."""
    transacao = conexao_admin.transaction()
    await transacao.start()
    try:
        await conexao_admin.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_sem_alteracao")
        await conexao_admin.execute(
            "UPDATE audit_log SET dados = '{\"forjado\": true}' WHERE tenant_id = $1 AND seq = 1",
            dados.tenant_a,
        )
        resultado = verificar_cadeia(await _cadeia(conexao_admin, dados.tenant_a))
        assert (resultado.integra, resultado.quebra_em, resultado.motivo) == (
            False,
            1,
            "conteudo alterado",
        )
    finally:
        await transacao.rollback()


async def test_rls_isola_a_trilha(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(conexao_app, _contexto(dados.tenant_a))
        tenants = {
            r["tenant_id"] for r in await conexao_app.fetch("SELECT tenant_id FROM audit_log")
        }
        assert tenants == {dados.tenant_a}
        with pytest.raises(NEGADO):
            await audit.registrar_asyncpg(
                conexao_app, tenant_id=dados.tenant_b, acao="teste.invasao", entidade="tenant"
            )


async def test_insercoes_concorrentes_mantem_a_cadeia(
    banco: Banco, conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    antes = len(await _cadeia(conexao_admin, dados.tenant_b))

    async def gravar(indice: int) -> None:
        conexao = await asyncpg.connect(banco.dsn_app)
        try:
            for repeticao in range(5):
                async with conexao.transaction():
                    await aplicar_contexto(conexao, _contexto(dados.tenant_b))
                    await audit.registrar_asyncpg(
                        conexao,
                        tenant_id=dados.tenant_b,
                        acao="teste.concorrencia",
                        entidade="tenant",
                        dados={"conexao": indice, "repeticao": repeticao},
                    )
        finally:
            await conexao.close()

    await asyncio.gather(*(gravar(i) for i in range(6)))
    cadeia = await _cadeia(conexao_admin, dados.tenant_b)
    assert len(cadeia) == antes + 30
    assert [linha.seq for linha in cadeia] == list(range(1, len(cadeia) + 1))
    assert verificar_cadeia(cadeia).integra
