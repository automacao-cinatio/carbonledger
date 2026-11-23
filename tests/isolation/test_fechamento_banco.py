"""Fechamento no banco: condicoes, hash, congelamento, reapresentacao e versoes.

FICTICIO: usa a referencia ficticia do calculo (gas natural de teste, 2 kg CO2/m3).
No tenant A, a fonte da semente sai da materialidade e entra uma fonte anual de gas natural
com 10 m3 aprovados: o calculo fica sem pendencias e o inventario pode fechar.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.core.erros import ErroValidacao
from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from app.modules.calculo import servico as calculo
from app.modules.fechamento import servico as fechamento
from tests.isolation.conftest import Banco, Dados
from tests.isolation.portal import CONJUNTO_GWP_DE_TESTE, carregar_referencia_de_calculo

pytestmark = pytest.mark.integration

NOME_EMPRESA = "Empresa A1"


@pytest.fixture(scope="module", autouse=True)
def _referencia(banco: Banco) -> None:
    asyncio.run(carregar_referencia_de_calculo(banco.dsn_curadoria))


async def _lancar_aprovado(conexao: asyncpg.Connection, dados: Dados, fonte: UUID) -> UUID:
    lancamento = uuid4()
    await conexao.execute(
        "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo, versao,"
        " quantidade, unidade, origem, criado_por) VALUES ($1, $2, $3, $4, 0, 1, 10, 'm**3',"
        " 'manual', $5)",
        dados.tenant_a,
        lancamento,
        dados.empresa_a1,
        fonte,
        dados.responsavel_a1,
    )
    await conexao.execute(
        "INSERT INTO lancamento_decisao (tenant_id, lancamento_id, empresa_id, decisao,"
        " decidido_por) VALUES ($1, $2, $3, 'aprovado', $4)",
        dados.tenant_a,
        lancamento,
        dados.empresa_a1,
        dados.admin_a,
    )
    return lancamento


@pytest.fixture
async def pronto(
    conexao_app: asyncpg.Connection, dados: Dados
) -> AsyncIterator[tuple[asyncpg.Connection, UUID]]:
    """Inventario A1 com calculo sem pendencias, numa transacao sempre desfeita."""
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(
            conexao_app,
            ContextoTenant(
                tenant_id=dados.tenant_a, escopo=Escopo.TENANT, usuario_id=dados.admin_a
            ),
        )
        await conexao_app.execute(
            "UPDATE fonte_emissao SET material = false,"
            " justificativa = 'Fora do inventario no teste de fechamento' WHERE fonte_id = $1",
            dados.fonte_a1,
        )
        await conexao_app.execute(
            "UPDATE inventario SET gwp_conjunto = $1 WHERE inventario_id = $2",
            CONJUNTO_GWP_DE_TESTE,
            dados.inventario_a1,
        )
        gas = uuid4()
        await conexao_app.execute(
            "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id, unidade_id,"
            " categoria, descricao, unidade_medida, periodicidade, atividade)"
            " VALUES ($1, $2, $3, $4, $5, 'escopo1.combustao_estacionaria', 'Forno', 'm**3',"
            " 'anual', 'teste_calc.gas_natural')",
            dados.tenant_a,
            gas,
            dados.empresa_a1,
            dados.inventario_a1,
            dados.unidade_a1,
        )
        await _lancar_aprovado(conexao_app, dados, gas)
        await calculo.calcular_inventario(
            conexao_app, dados.tenant_a, dados.inventario_a1, dados.admin_a
        )
        yield conexao_app, gas
    finally:
        await transacao.rollback()


async def _fechar(conexao: asyncpg.Connection, dados: Dados, nome: str = NOME_EMPRESA) -> object:
    return await fechamento.fechar(
        conexao,
        tenant_id=dados.tenant_a,
        usuario_id=dados.admin_a,
        inventario_id=dados.inventario_a1,
        confirmacao=nome,
        nome_empresa=NOME_EMPRESA,
    )


async def _sqlstate(conexao: asyncpg.Connection, sql: str, *argumentos: object) -> str:
    with pytest.raises(asyncpg.PostgresError) as erro:
        async with conexao.transaction():
            await conexao.execute(sql, *argumentos)
    return str(erro.value.sqlstate)


async def test_condicoes_e_fechamento(
    pronto: tuple[asyncpg.Connection, UUID], dados: Dados
) -> None:
    conexao, _gas = pronto
    situacao = await fechamento.situacao(conexao, dados.inventario_a1)
    assert [(c.nome, c.ok) for c in situacao.checagens] == [
        ("calculo", True),
        ("atualizado", True),
        ("pendencias", True),
        ("conferido", True),
    ]
    assert situacao.avisos == ["1 período(s) sem evidência. O verificador vai pedir os documentos."]
    with pytest.raises(ErroValidacao, match="digite exatamente o nome"):
        await _fechar(conexao, dados, "empresa a1")
    evento = await _fechar(conexao, dados)
    assert evento.versao == 1 and len(evento.hash_fechamento) == 32  # type: ignore[attr-defined]
    assert (
        await conexao.fetchval(
            "SELECT estado FROM inventario WHERE inventario_id = $1", dados.inventario_a1
        )
        == "fechado"
    )
    # O hash e reproduzivel: mesmo calculo, mesmos dados, mesmas evidencias.
    refeito = await fechamento.hash_do_fechamento(
        conexao,
        dados.inventario_a1,
        1,
        evento.calculo_id,  # type: ignore[attr-defined]
    )
    assert refeito == evento.hash_fechamento  # type: ignore[attr-defined]


async def test_calculo_desatualizado_nao_fecha(
    pronto: tuple[asyncpg.Connection, UUID], dados: Dados
) -> None:
    conexao, gas = pronto
    # Uma correcao depois do calculo: o hash da entrada de agora nao bate mais.
    await conexao.execute(
        "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo, versao,"
        " quantidade, unidade, origem, criado_por) VALUES ($1, gen_random_uuid(), $2, $3, 0, 2,"
        " 11, 'm**3', 'manual', $4)",
        dados.tenant_a,
        dados.empresa_a1,
        gas,
        dados.responsavel_a1,
    )
    situacao = await fechamento.situacao(conexao, dados.inventario_a1)
    assert not situacao.pode_fechar
    assert {c.nome for c in situacao.checagens if not c.ok} == {"atualizado"}
    with pytest.raises(ErroValidacao, match="Calcule de novo"):
        await _fechar(conexao, dados)


async def test_fechado_congela_tudo(pronto: tuple[asyncpg.Connection, UUID], dados: Dados) -> None:
    conexao, gas = pronto
    await _fechar(conexao, dados)
    assert (
        await _sqlstate(
            conexao,
            "UPDATE inventario SET abordagem = 'controle_financeiro' WHERE inventario_id = $1",
            dados.inventario_a1,
        )
        == "CL020"
    )
    assert (
        await _sqlstate(
            conexao, "UPDATE fonte_emissao SET descricao = 'X' WHERE fonte_id = $1", gas
        )
        == "CL020"
    )
    assert (
        await _sqlstate(
            conexao,
            "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo,"
            " versao, quantidade, unidade, origem) VALUES ($1, gen_random_uuid(), $2, $3, 0, 9,"
            " 1, 'm**3', 'manual')",
            dados.tenant_a,
            dados.empresa_a1,
            gas,
        )
        == "CL011"
    )
    assert (
        await _sqlstate(
            conexao,
            "INSERT INTO evidencia (tenant_id, evidencia_id, empresa_id, fonte_id, nome_arquivo,"
            " tipo_conteudo, tamanho, sha256, chave_objeto) VALUES ($1, gen_random_uuid(), $2, $3,"
            " 'a.pdf', 'application/pdf', 1, $4, 'x')",
            dados.tenant_a,
            dados.empresa_a1,
            gas,
            bytes(32),
        )
        == "CL020"
    )
    with pytest.raises(asyncpg.PostgresError) as erro:
        async with conexao.transaction():
            await calculo.calcular_inventario(
                conexao, dados.tenant_a, dados.inventario_a1, dados.admin_a
            )
    assert erro.value.sqlstate == "CL020"


async def test_estado_nao_muda_sem_evento(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(
            conexao_app, ContextoTenant(tenant_id=dados.tenant_a, escopo=Escopo.TENANT)
        )
        assert (
            await _sqlstate(
                conexao_app,
                "UPDATE inventario SET estado = 'fechado' WHERE inventario_id = $1",
                dados.inventario_a1,
            )
            == "CL021"
        )


async def test_reapresentacao_cria_versao_nova(
    pronto: tuple[asyncpg.Connection, UUID], dados: Dados
) -> None:
    conexao, _gas = pronto
    primeiro = await _fechar(conexao, dados)
    argumentos = {
        "tenant_id": dados.tenant_a,
        "usuario_id": dados.admin_a,
        "inventario_id": dados.inventario_a1,
    }
    with pytest.raises(ErroValidacao, match="20 a 1000"):
        await fechamento.reabrir(conexao, **argumentos, motivo="curto")
    await fechamento.reabrir(
        conexao, **argumentos, motivo="Verificador apontou nota fiscal de dezembro faltando."
    )
    situacao = await fechamento.situacao(conexao, dados.inventario_a1)
    assert situacao.estado == "em_coleta" and situacao.proxima_versao == 2
    segundo = await _fechar(conexao, dados)
    assert segundo.versao == 2  # type: ignore[attr-defined]
    assert segundo.hash_fechamento != primeiro.hash_fechamento  # type: ignore[attr-defined]
    tipos = [e.tipo for e in (await fechamento.situacao(conexao, dados.inventario_a1)).eventos]
    assert tipos[-3:] == ["fechamento", "reabertura", "fechamento"]
    acoes = await conexao.fetch(
        "SELECT acao FROM audit_log WHERE acao LIKE 'inventario.%' AND entidade_id = $1"
        " ORDER BY seq",
        str(dados.inventario_a1),
    )
    assert [a["acao"] for a in acoes][-3:] == [
        "inventario.fechado",
        "inventario.reaberto",
        "inventario.fechado",
    ]
