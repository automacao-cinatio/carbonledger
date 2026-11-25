"""Cobranca no banco (P22): fatura do mes uma vez so, vencimento, inadimplencia, pagamento por
evento do gateway (uma vez so), localizacao pelo webhook, periodo de teste e privilegios."""

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID

import asyncpg
import pytest

from app.core.tenancy import ContextoTenant, Escopo, aplicar_contexto
from app.modules.cobranca import precos, servico
from app.modules.cobranca.gateway import GatewaySimulado, do_simulador
from tests.isolation.conftest import Dados

pytestmark = pytest.mark.integration

PARAMETROS = servico.Parametros(
    faixas=precos.ler_tabela("3:390,00;*:190,00"),
    dias_teste=30,
    dias_vencimento=10,
    dias_tolerancia=15,
)
GATEWAY = GatewaySimulado("http://localhost:8000", "token-de-teste")


async def _em_tenant(conexao: asyncpg.Connection, tenant: UUID) -> AsyncIterator[None]:
    transacao = conexao.transaction()
    await transacao.start()
    try:
        await aplicar_contexto(conexao, ContextoTenant(tenant_id=tenant, escopo=Escopo.TENANT))
        yield
    finally:
        await transacao.rollback()


async def test_ciclo_da_fatura(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    async for _ in _em_tenant(conexao_app, dados.tenant_a):
        # Marco: fatura das 2 empresas ativas, uma vez so.
        marco = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 5), PARAMETROS, GATEWAY
        )
        assert marco.fatura_emitida == servico.id_da_fatura(dados.tenant_a, date(2026, 3, 1))
        fatura = await conexao_app.fetchrow(
            "SELECT * FROM fatura WHERE fatura_id = $1", marco.fatura_emitida
        )
        assert fatura["empresas_ativas"] == 2 and fatura["valor_centavos"] == 78000
        assert fatura["vencimento"] == date(2026, 3, 15) and fatura["situacao"] == "aberta"
        assert fatura["url_pagamento"].endswith(
            f"/cobranca/simulador/{fatura['gateway_cobranca_id']}"
        )
        de_novo = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 6), PARAMETROS, GATEWAY
        )
        assert de_novo.fatura_emitida is None
        assert (
            await conexao_app.fetchval(
                "SELECT count(*) FROM fatura WHERE competencia = '2026-03-01'"
            )
            == 1
        )

        # Passou do vencimento: vencida, ainda dentro da tolerancia.
        vencida = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 3, 20), PARAMETROS, GATEWAY
        )
        assert vencida.vencidas == 1 and vencida.situacao == "ativa"

        # Passou da tolerancia: inadimplente; abril tambem e faturado.
        abril = await servico.rodada(
            conexao_app, dados.tenant_a, date(2026, 4, 1), PARAMETROS, GATEWAY
        )
        assert abril.situacao == "inadimplente" and abril.fatura_emitida is not None
        assert await conexao_app.fetchval("SELECT situacao FROM assinatura") == "inadimplente"

        # O webhook acha o tenant e a fatura pela cobranca (funcao do app_identidade).
        achado = await servico.localizar(conexao_app, "simulado", fatura["gateway_cobranca_id"])
        assert achado == (dados.tenant_a, marco.fatura_emitida)
        assert await servico.localizar(conexao_app, "simulado", "nao-existe") is None

        # Pagamento de marco: paga e, sem outra vencida, a assinatura volta a ativa.
        evento = do_simulador(fatura["gateway_cobranca_id"])
        assert await servico.registrar_evento(
            conexao_app, dados.tenant_a, marco.fatura_emitida, "simulado", evento
        )
        assert (
            await conexao_app.fetchval(
                "SELECT situacao FROM fatura WHERE fatura_id = $1", marco.fatura_emitida
            )
            == "paga"
        )
        assert await conexao_app.fetchval("SELECT situacao FROM assinatura") == "ativa"
        # O mesmo evento de novo nao faz nada.
        assert not await servico.registrar_evento(
            conexao_app, dados.tenant_a, marco.fatura_emitida, "simulado", evento
        )
        acoes = [
            linha["acao"]
            for linha in await conexao_app.fetch(
                "SELECT acao FROM audit_log WHERE acao LIKE 'cobranca.%' ORDER BY seq"
            )
        ]
        assert acoes == [
            "cobranca.fatura_emitida",
            "cobranca.situacao_alterada",
            "cobranca.fatura_emitida",
            "cobranca.fatura_paga",
            "cobranca.situacao_alterada",
        ]


async def test_periodo_de_teste_nao_fatura(conexao_app: asyncpg.Connection, dados: Dados) -> None:
    async for _ in _em_tenant(conexao_app, dados.tenant_b):
        await conexao_app.execute(
            "UPDATE assinatura SET situacao = 'teste', teste_ate = '2030-01-01'"
        )
        rodada = await servico.rodada(
            conexao_app, dados.tenant_b, date(2026, 3, 5), PARAMETROS, GATEWAY
        )
        assert rodada.fatura_emitida is None and rodada.situacao == "teste"


async def test_fatura_so_muda_situacao_e_gateway(conexao_admin: asyncpg.Connection) -> None:
    for coluna, esperado in (
        ("situacao", True),
        ("paga_em", True),
        ("url_pagamento", True),
        ("valor_centavos", False),
        ("competencia", False),
        ("empresas_ativas", False),
    ):
        tem = await conexao_admin.fetchval(
            "SELECT has_column_privilege('app_user', 'fatura', $1, 'UPDATE')", coluna
        )
        assert tem is esperado, coluna
