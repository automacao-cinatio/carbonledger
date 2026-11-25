"""Cobranca no portal (P22): tela do administrador, fatura emitida pela rodada, pagamento pelo
simulador e o webhook (token, cobranca desconhecida, repeticao)."""

import asyncio
import json
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.core.tenancy import ContextoTenant, Escopo, conexao_de_tenant
from app.modules.cobranca import servico
from tests.isolation.conftest import Banco
from tests.isolation.portal import csrf, entrar

pytestmark = pytest.mark.integration

TOKEN = "token-de-teste"  # noqa: S105  # o mesmo do app de teste (conftest)


async def _rodada(banco: Banco, tenant_id: UUID, hoje: date) -> servico.Rodada:
    configuracao = obter_configuracao()
    parametros = servico.parametros_da_configuracao(configuracao)
    gateway = servico.gateway_da_configuracao(configuracao)
    assert parametros is not None and gateway is not None
    contexto = ContextoTenant(tenant_id=tenant_id, escopo=Escopo.TENANT)
    async with conexao_de_tenant(banco.dsn_app, contexto) as conexao:
        return await servico.rodada(conexao, tenant_id, hoje, parametros, gateway)


def _webhook(cliente: TestClient, corpo: dict[str, Any], token: str | None = TOKEN) -> Any:
    """Chamada do gateway: sem cookie de sessao (com cookie, o CSRF recusaria antes)."""
    cliente.cookies.clear()
    cabecalhos = {"content-type": "application/json"}
    if token is not None:
        cabecalhos["X-Cobranca-Token"] = token
    return cliente.post("/cobranca/webhook/simulado", content=json.dumps(corpo), headers=cabecalhos)


def test_roteiro_da_cobranca(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Pagante", "dona@pagante.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-pagante", email="dona@pagante.example")
    cabecalho = csrf(cliente)
    assert 'href="/cobranca"' in cliente.get("/").text
    pagina = cliente.get("/cobranca").text
    assert "1 a 3" in pagina and "4 em diante" in pagina and "390,00" in pagina
    assert "A assinatura começa na primeira rodada" in pagina

    cliente.post("/organizacao/grupos", data={"nome": "Grupo P"}, headers=cabecalho)
    grupo = re.search(  # type: ignore[union-attr]
        r"/organizacao/grupos/([0-9a-f-]{36})/editar", cliente.get("/organizacao").text
    ).group(1)
    cliente.post(
        "/organizacao/empresas",
        data={"grupo_id": grupo, "cnpj": "11.222.333/0001-81", "nome": "Pagante S.A.",
              "uf": "SP", "municipio": "Campinas"},
        headers=cabecalho,
    )  # fmt: skip

    # No teste: assinatura criada, nada faturado. Depois do teste: fatura da empresa ativa.
    hoje = date.today()
    no_teste = asyncio.run(_rodada(banco, tenant_id, hoje))
    assert no_teste.situacao == "teste" and no_teste.fatura_emitida is None
    depois = asyncio.run(_rodada(banco, tenant_id, hoje + timedelta(days=40)))
    assert depois.fatura_emitida is not None and depois.situacao == "ativa"
    pagina = cliente.get("/cobranca").text
    assert "Aberta" in pagina and "390,00" in pagina
    caminho = re.search(
        r'href="http://testserver(/cobranca/simulador/(sim_cob_[0-9a-f-]+))"', pagina
    )
    assert caminho is not None
    cobranca_id = caminho.group(2)

    # Pagamento pelo simulador (dev): a fatura fica paga.
    simulador = cliente.get(caminho.group(1))
    assert simulador.status_code == 200 and "Pagar (simulado)" in simulador.text
    pago = cliente.post(f"{caminho.group(1)}/pagar", headers=csrf(cliente))
    assert pago.status_code == 204 and unquote(pago.headers["x-aviso"]).startswith("Pagamento")
    assert "Paga" in cliente.get("/cobranca").text
    assert "Fatura paga" in cliente.get(caminho.group(1)).text

    # Webhook (sem sessao): sem token, token errado, cobranca desconhecida, outro gateway: 404.
    evento = {"id": "evt-1", "tipo": "pagamento_confirmado", "cobranca_id": cobranca_id}
    errado = "errado"
    assert _webhook(cliente, evento, token=None).status_code == 404
    assert _webhook(cliente, evento, token=errado).status_code == 404
    assert _webhook(cliente, {**evento, "cobranca_id": "sim_cob_x"}).status_code == 404
    cliente.cookies.clear()
    outro_gateway = cliente.post(
        "/cobranca/webhook/asaas", content=b"{}", headers={"X-Cobranca-Token": TOKEN}
    )
    assert outro_gateway.status_code == 404
    # Valido, e repetido: 200 e nada muda (o evento e registrado uma vez).
    assert _webhook(cliente, evento).status_code == 200
    assert _webhook(cliente, evento).status_code == 200
    eventos = asyncio.run(_eventos(banco, tenant_id))
    assert eventos.count("evt-1") == 1


async def _eventos(banco: Banco, tenant_id: UUID) -> list[str]:
    import asyncpg

    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        return [
            linha["evento_id"]
            for linha in await conexao.fetch(
                "SELECT evento_id FROM cobranca_evento WHERE tenant_id = $1", tenant_id
            )
        ]
    finally:
        await conexao.close()
