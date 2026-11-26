"""Unidade fora do limite organizacional (0% na abordagem do inventario) no banco.

A regra em SQL (SQL_DENTRO_DO_LIMITE) precisa bater com a de Python (dentro_do_limite), e as
fontes dessa unidade nao cobram dado na campanha, nao pedem evidencia no fechamento nem geram
pendencia no calculo.

Semente do tenant A: inventario A1 por controle operacional, unidade A1 com os dois
controles e fonte mensal Caldeira so com janeiro aprovado.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from itertools import product

import asyncpg
import pytest

from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from app.modules.calculo import servico as calculo
from app.modules.coleta import campanhas
from app.modules.fechamento import servico as fechamento
from app.modules.organizacao.limites import (
    SQL_DENTRO_DO_LIMITE,
    Abordagem,
    FatosDaUnidade,
    dentro_do_limite,
)
from tests.isolation.conftest import Dados

pytestmark = pytest.mark.integration


@pytest.fixture
async def tenant_a(
    conexao_app: asyncpg.Connection, dados: Dados
) -> AsyncIterator[asyncpg.Connection]:
    transacao = conexao_app.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(
            conexao_app, ContextoTenant(tenant_id=dados.tenant_a, escopo=Escopo.TENANT)
        )
        yield conexao_app
    finally:
        await transacao.rollback()


async def test_regra_sql_bate_com_a_de_python(conexao_app: asyncpg.Connection) -> None:
    consulta = (
        f"SELECT {SQL_DENTRO_DO_LIMITE}"  # noqa: S608  # nosec B608
        " FROM (SELECT $1::text AS abordagem) i,"
        " (SELECT $2::numeric AS participacao_societaria, $3::boolean AS controle_operacional,"
        " $4::boolean AS controle_financeiro) u"
    )
    for abordagem, participacao, operacional, financeiro in product(
        Abordagem, (Decimal(0), Decimal("0.01"), Decimal(100)), (True, False), (True, False)
    ):
        no_banco = await conexao_app.fetchval(
            consulta, abordagem.value, participacao, operacional, financeiro
        )
        fatos = FatosDaUnidade(participacao, operacional, financeiro)
        assert no_banco is dentro_do_limite(abordagem, fatos), (abordagem, fatos)


async def _tirar_controle(conexao: asyncpg.Connection, dados: Dados) -> None:
    await conexao.execute(
        "UPDATE unidade SET controle_operacional = false WHERE unidade_id = $1", dados.unidade_a1
    )


async def test_campanha_nao_cobra_fonte_fora_do_limite(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    assert await campanhas.pendencias(tenant_a, dados.inventario_a1)
    await _tirar_controle(tenant_a, dados)
    assert await campanhas.pendencias(tenant_a, dados.inventario_a1) == []


async def test_fechamento_nao_pede_evidencia_de_fonte_fora_do_limite(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    assert await fechamento._periodos_sem_evidencia(tenant_a, dados.inventario_a1) > 0
    await _tirar_controle(tenant_a, dados)
    assert await fechamento._periodos_sem_evidencia(tenant_a, dados.inventario_a1) == 0


async def test_calculo_sem_pendencia_de_fonte_fora_do_limite(
    tenant_a: asyncpg.Connection, dados: Dados
) -> None:
    agora = datetime.now(UTC)
    _inv, antes = await calculo.montar_entrada(tenant_a, dados.inventario_a1, agora)
    assert any(p.fonte_id == dados.fonte_a1 for p in antes.pendencias)
    await _tirar_controle(tenant_a, dados)
    _inv, depois = await calculo.montar_entrada(tenant_a, dados.inventario_a1, agora)
    assert not any(p.fonte_id == dados.fonte_a1 for p in depois.pendencias)
    assert not any(i.fonte_id == dados.fonte_a1 for i in depois.itens)
