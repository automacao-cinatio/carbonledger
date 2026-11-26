"""Bloqueio por inadimplencia (so acoes novas): aviso em todas as telas; abrir inventario,
fechar, gerar relatorio, convidar pessoas e dar acesso a verificador recusados; lancar dados
continua; pago, libera."""

import asyncio
import re
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.modules.cobranca.bloqueio import MENSAGEM
from tests.isolation.conftest import Banco
from tests.isolation.portal import csrf, entrar

pytestmark = pytest.mark.integration


async def _situacao(banco: Banco, tenant_id: UUID, situacao: str) -> None:
    conexao = await asyncpg.connect(banco.dsn_admin)
    try:
        await conexao.execute(
            "INSERT INTO assinatura (tenant_id, situacao, teste_ate, gateway)"
            " VALUES ($1, $2, '2020-01-31', 'simulado')"
            " ON CONFLICT (tenant_id) DO UPDATE SET situacao = EXCLUDED.situacao",
            tenant_id,
            situacao,
        )
    finally:
        await conexao.close()


def _bloqueado(resposta: Any) -> bool:
    return resposta.status_code == 422 and resposta.json()["erros"][0]["mensagem"] == MENSAGEM


def test_inadimplente_bloqueia_so_acoes_novas(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Atrasado", "dona@atrasado.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-atrasado", email="dona@atrasado.example")
    cab = csrf(cliente)
    cliente.post("/organizacao/grupos", data={"nome": "Grupo A"}, headers=cab)
    grupo = re.search(  # type: ignore[union-attr]
        r"/organizacao/grupos/([0-9a-f-]{36})/editar", cliente.get("/organizacao").text
    ).group(1)
    empresa = cliente.post(
        "/organizacao/empresas",
        data={"grupo_id": grupo, "cnpj": "11.222.333/0001-81", "nome": "Atrasada S.A.",
              "uf": "SP", "municipio": "Campinas"},
        headers=cab,
    ).headers["hx-redirect"].rsplit("/", 1)[-1]  # fmt: skip
    cliente.post(
        f"/organizacao/empresas/{empresa}/unidades",
        data={"nome": "Base", "uf": "SP", "municipio": "Campinas",
              "participacao_societaria": "100", "controle_operacional": "sim"},
        headers=cab,
    )  # fmt: skip
    unidade = re.search(  # type: ignore[union-attr]
        r"/organizacao/unidades/([0-9a-f-]{36})/editar",
        cliente.get(f"/organizacao/empresas/{empresa}").text,
    ).group(1)
    inventario = (
        cliente.post(
            f"/coleta/empresas/{empresa}/inventarios",
            data={"ano": "2025", "abordagem": "controle_operacional"},
            headers=cab,
        )
        .headers["hx-redirect"]
        .rsplit("/", 1)[-1]
    )
    fonte = cliente.post(
        f"/coleta/inventarios/{inventario}/fontes",
        data={"unidade_id": unidade, "categoria": "escopo1.combustao_estacionaria",
              "descricao": "Caldeira", "unidade_medida": "L", "periodicidade": "mensal",
              "material": "sim"},
        headers=cab,
    ).headers["hx-redirect"].rsplit("/", 1)[-1]  # fmt: skip

    # Em dia: sem aviso.
    assert "Pagamento em atraso" not in cliente.get("/").text

    asyncio.run(_situacao(banco, tenant_id, "inadimplente"))
    assert "Pagamento em atraso" in cliente.get("/").text
    assert "Regularize na tela Cobrança" in cliente.get("/coleta").text
    # Acoes novas: recusadas, com a mensagem que diz como resolver.
    assert _bloqueado(
        cliente.post(f"/coleta/empresas/{empresa}/inventarios",
                     data={"ano": "2024", "abordagem": "controle_operacional"}, headers=cab)
    )  # fmt: skip
    assert _bloqueado(
        cliente.post("/usuarios/convites", data={"email": "x@atrasado.example",
                                                 "perfil": "gestor"}, headers=cab)
    )  # fmt: skip
    assert _bloqueado(
        cliente.post(f"/fechamento/inventarios/{inventario}/fechar",
                     data={"confirmacao": "Atrasada S.A."}, headers=cab)
    )  # fmt: skip
    assert _bloqueado(cliente.post(f"/relatorios/inventarios/{inventario}/gerar", headers=cab))
    # Acesso novo de verificador tambem (decisao do usuario em 2026-11-26).
    assert _bloqueado(
        cliente.post("/verificacao/concessoes", data={"inventario_id": inventario,
                                                      "email": "v@verifica.example"}, headers=cab)
    )  # fmt: skip
    # O trabalho em andamento continua: lancar dado e consultar.
    lancado = cliente.post(
        f"/coleta/fontes/{fonte}/periodos/1/lancamentos", data={"quantidade": "100"}, headers=cab
    )
    assert lancado.status_code == 204
    assert cliente.get("/resultados").status_code == 200

    # Pago: libera na hora.
    asyncio.run(_situacao(banco, tenant_id, "ativa"))
    assert "Pagamento em atraso" not in cliente.get("/").text
    liberado = cliente.post(
        f"/coleta/empresas/{empresa}/inventarios",
        data={"ano": "2024", "abordagem": "controle_operacional"},
        headers=cab,
    )
    assert liberado.status_code == 204
