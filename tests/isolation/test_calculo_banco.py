"""Calculo do escopo 1 no banco, com a referencia ficticia aprovada pela curadoria.

FICTICIO: fatores, mistura e GWP de tests/dados_ficticios/calculo_*.csv. Caso do tenant A da
semente: 100 L de diesel comercial em janeiro (aprovado), mistura 85/15, unidade com controle
operacional (100%).
  fossil 85 L: CO2 221 kg = 0,221 t; CH4 0,034 kg x 28 = 0,000952 tCO2e
  bio    15 L: CO2 biogenico 36 kg = 0,036 t; CH4 0,0045 kg x 28 = 0,000126 tCO2e
  escopo 1 = 0,222078 tCO2e; biogenico = 0,036 t; fevereiro a dezembro sem dado.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest

from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from app.modules.calculo import servico
from tests.isolation.conftest import Banco, Dados
from tests.isolation.portal import CONJUNTO_GWP_DE_TESTE, carregar_referencia_de_calculo

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _referencia(banco: Banco) -> None:
    asyncio.run(carregar_referencia_de_calculo(banco.dsn_curadoria))


@pytest.fixture
async def tenant_a(
    conexao_app: asyncpg.Connection, dados: Dados
) -> AsyncIterator[asyncpg.Connection]:
    """Transacao do tenant A, com a fonte da semente pronta para calcular; sempre desfeita."""
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
            "UPDATE fonte_emissao SET atividade = 'teste_calc.diesel_comercial'"
            " WHERE fonte_id = $1",
            dados.fonte_a1,
        )
        await conexao_app.execute(
            "UPDATE inventario SET gwp_conjunto = $1 WHERE inventario_id = $2",
            CONJUNTO_GWP_DE_TESTE,
            dados.inventario_a1,
        )
        yield conexao_app
    finally:
        await transacao.rollback()


async def test_calcula_o_caso_da_semente(tenant_a: asyncpg.Connection, dados: Dados) -> None:
    execucao = await servico.calcular_inventario(
        tenant_a, dados.tenant_a, dados.inventario_a1, dados.admin_a
    )
    resultado = execucao.resultado
    assert resultado.escopo1_tco2e == Decimal("0.222078")
    assert resultado.biogenico_tco2 == Decimal("0.036")
    assert [p.motivo for p in resultado.pendencias] == ["falta_dado"] * 11
    gravado = await tenant_a.fetchrow(
        "SELECT escopo1_tco2e, linhas, pendencias, metodo, metodo_versao FROM calculo"
        " WHERE calculo_id = $1",
        execucao.calculo_id,
    )
    assert gravado["escopo1_tco2e"] == Decimal("0.222078")
    assert (gravado["linhas"], gravado["pendencias"]) == (4, 11)
    assert (gravado["metodo"], gravado["metodo_versao"]) == ("fator_direto", "1.3.0")
    soma = await tenant_a.fetchval(
        "SELECT sum(tco2e) FROM calculo_linha WHERE calculo_id = $1", execucao.calculo_id
    )
    assert soma == Decimal("0.222078")
    # A memoria guarda a versao de cada fator e a data de referencia (fim do mes, sem compra).
    referencias = await tenant_a.fetch(
        "SELECT DISTINCT data_referencia, mistura_versao_id IS NOT NULL AS com_mistura"
        " FROM calculo_linha WHERE calculo_id = $1",
        execucao.calculo_id,
    )
    assert [(r["data_referencia"].isoformat(), r["com_mistura"]) for r in referencias] == [
        ("2025-01-31", True)
    ]
    trilha = await tenant_a.fetchval(
        "SELECT count(*) FROM audit_log WHERE acao = 'calculo.executado' AND entidade_id = $1",
        str(execucao.calculo_id),
    )
    assert trilha == 1


async def test_refazer_da_o_mesmo_hash(tenant_a: asyncpg.Connection, dados: Dados) -> None:
    execucao = await servico.calcular_inventario(
        tenant_a, dados.tenant_a, dados.inventario_a1, dados.admin_a
    )
    conferencia = await servico.refazer(tenant_a, execucao.calculo_id)
    assert conferencia.ok
    # Calcular de novo, sem mudar nada, cria outra execucao com o mesmo resultado.
    outra = await servico.calcular_inventario(
        tenant_a, dados.tenant_a, dados.inventario_a1, dados.admin_a
    )
    assert outra.calculo_id != execucao.calculo_id
    assert outra.resultado.hash_resultado == execucao.resultado.hash_resultado


async def test_conhecimento_antes_da_aprovacao_nao_ve_o_fator(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    """Bitemporal: no ano 2000 os fatores ficticios ainda nao tinham sido aprovados."""
    _inventario, entrada = await servico.montar_entrada(
        tenant_a, dados.inventario_a1, datetime(2000, 1, 1, tzinfo=UTC)
    )
    assert entrada.itens == ()
    assert "sem_fator" in {p.motivo for p in entrada.pendencias}


async def test_sem_atividade_e_sem_fator_viram_pendencia(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    await tenant_a.execute(
        "UPDATE fonte_emissao SET atividade = NULL WHERE fonte_id = $1", dados.fonte_a1
    )
    execucao = await servico.calcular_inventario(
        tenant_a, dados.tenant_a, dados.inventario_a1, dados.admin_a
    )
    assert [p.motivo for p in execucao.resultado.pendencias] == ["sem_atividade"]
    assert execucao.resultado.escopo1_tco2e == 0

    await tenant_a.execute(
        "UPDATE fonte_emissao SET atividade = 'teste_calc.inexistente' WHERE fonte_id = $1",
        dados.fonte_a1,
    )
    execucao = await servico.calcular_inventario(
        tenant_a, dados.tenant_a, dados.inventario_a1, dados.admin_a
    )
    assert {p.motivo for p in execucao.resultado.pendencias} == {"sem_fator", "falta_dado"}


async def test_consolidacao_pela_participacao(
    tenant_a: asyncpg.Connection, conexao_admin: asyncpg.Connection, dados: Dados
) -> None:
    await tenant_a.execute(
        "UPDATE unidade SET participacao_societaria = 40 WHERE unidade_id = $1", dados.unidade_a1
    )
    await tenant_a.execute(
        "UPDATE inventario SET abordagem = 'participacao_societaria' WHERE inventario_id = $1",
        dados.inventario_a1,
    )
    execucao = await servico.calcular_inventario(
        tenant_a, dados.tenant_a, dados.inventario_a1, dados.admin_a
    )
    assert execucao.resultado.escopo1_tco2e == Decimal("0.222078") * Decimal("0.4")


async def test_outro_tenant_nao_calcula_inventario_alheio(
    conexao_app: asyncpg.Connection, dados: Dados
) -> None:
    async with conexao_app.transaction():
        await aplicar_contexto(
            conexao_app, ContextoTenant(tenant_id=dados.tenant_b, escopo=Escopo.TENANT)
        )
        with pytest.raises(servico.InventarioInexistente):
            await servico.calcular_inventario(
                conexao_app, dados.tenant_b, dados.inventario_a1, None
            )


async def test_escopo_3_entra_com_total_proprio(tenant_a: asyncpg.Connection, dados: Dados) -> None:
    viagens = uuid4()
    await tenant_a.execute(
        "INSERT INTO fonte_emissao (tenant_id, fonte_id, empresa_id, inventario_id, unidade_id,"
        " categoria, descricao, unidade_medida, periodicidade, atividade)"
        " VALUES ($1, $2, $3, $4, $5, 'escopo3.cat06_viagens', 'Viagens', 'km', 'anual',"
        " 'teste_calc.voo')",
        dados.tenant_a,
        viagens,
        dados.empresa_a1,
        dados.inventario_a1,
        dados.unidade_a1,
    )
    lancamento = uuid4()
    await tenant_a.execute(
        "INSERT INTO lancamento (tenant_id, lancamento_id, empresa_id, fonte_id, periodo, versao,"
        " quantidade, unidade, origem, criado_por) VALUES ($1, $2, $3, $4, 0, 1, 1000, 'km',"
        " 'manual', $5)",
        dados.tenant_a,
        lancamento,
        dados.empresa_a1,
        viagens,
        dados.responsavel_a1,
    )
    await tenant_a.execute(
        "INSERT INTO lancamento_decisao (tenant_id, lancamento_id, empresa_id, decisao,"
        " decidido_por) VALUES ($1, $2, $3, 'aprovado', $4)",
        dados.tenant_a,
        lancamento,
        dados.empresa_a1,
        dados.admin_a,
    )
    execucao = await servico.calcular_inventario(
        tenant_a, dados.tenant_a, dados.inventario_a1, dados.admin_a
    )
    # 1.000 km x 0,15 kg/km = 0,15 tCO2e, fora do escopo 1.
    assert execucao.resultado.escopo3_tco2e == Decimal("0.15")
    assert execucao.resultado.escopo1_tco2e == Decimal("0.222078")
    gravado = await tenant_a.fetchval(
        "SELECT escopo3_tco2e FROM calculo WHERE calculo_id = $1", execucao.calculo_id
    )
    assert gravado == Decimal("0.15")
