"""Revisao de usabilidade (2026-11-25): com o inventario fechado, cada tela diz o que vem
depois (relatorio assinado, depois verificador), a jornada tem a etapa do relatorio e o
escopo 3, e o formulario sugere um ano que ainda nao tem inventario."""

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from app.web.renderizacao import MENU
from tests.isolation.conftest import Banco
from tests.isolation.portal import entrar
from tests.isolation.test_relatorios_portal import _inventario_fechado

pytestmark = pytest.mark.integration


def test_telas_sabem_que_o_inventario_fechou(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Usavel", "dona@usavel.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-usavel", email="dona@usavel.example")
    asyncio.run(_inventario_fechado(banco, tenant_id))

    inicio = cliente.get("/").text
    assert "Inventário 2025 fechado." in inicio and "Inventário 2025 aberto" not in inicio
    assert "Relatório assinado" in inicio and "Gere o relatório assinado." in inicio
    assert "Escopo 3 (último cálculo)" in inicio
    # O proximo passo e a primeira etapa nao feita: o relatorio, antes do verificador.
    assert re.search(r"Próximo passo.*Relatório assinado: Gere o relatório", inicio, re.S)

    coleta = cliente.get("/coleta").text
    assert "Inventário 2025 fechado" in coleta and 'href="/relatorios"' in coleta
    assert "Continue a coleta" not in coleta
    assert re.search(r'id="campo-ano"[^>]*value="2024"', coleta) or 'value="2024"' in coleta

    resultados = cliente.get("/resultados").text
    assert "Inventário fechado com este cálculo" in resultados
    assert 'href="/relatorios"' in resultados

    fechamento = cliente.get("/fechamento").text
    assert "Gere o relatório assinado da empresa." in fechamento


def test_menu_poe_relatorios_antes_da_verificacao() -> None:
    urls = [item.url for item in MENU]
    assert urls.index("/fechamento") < urls.index("/relatorios") < urls.index("/verificacao")
