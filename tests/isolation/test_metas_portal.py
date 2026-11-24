"""Metas de ponta a ponta: criar a partir do inventario fechado, acompanhar e planejar acoes.

O inventario 2025 chega fechado pela semente do teste do verificador (calculo com escopo 1 de
1,5 tCO2e e escopo 2 de 0). Meta de 40% nos escopos 1 e 2, de 2025 a 2030:
  alvo = 1,5 x 0,6 = 0,9 tCO2e; trajetoria em 2027 = 1,5 - 0,6 x 2/5 = 1,26 tCO2e.
"""

import asyncio
import re
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.cli import criar_tenant
from app.core.config import obter_configuracao
from tests.isolation.conftest import Banco
from tests.isolation.portal import csrf, entrar
from tests.isolation.test_verificacao_portal import _dono, _inventario_fechado

pytestmark = pytest.mark.integration


def _campos(resposta: Any) -> set[str]:
    assert resposta.status_code == 422, resposta.text
    return {e["campo"] for e in resposta.json()["erros"]}


def _aviso(resposta: Any) -> str:
    assert resposta.status_code == 204, resposta.text
    return unquote(resposta.headers["x-aviso"])


def test_roteiro_das_metas(app_cliente: TestClient, banco: Banco) -> None:
    cliente = app_cliente
    tenant_id, token = asyncio.run(
        criar_tenant(obter_configuracao(), "Cliente Metas", "dona@metas.example")
    )
    entrar(cliente, f"/convite/{token}/dev", sujeito="dona-metas", email="dona@metas.example")
    ids = asyncio.run(_inventario_fechado(banco, tenant_id, asyncio.run(_dono(banco, tenant_id))))
    cabecalho = csrf(cliente)

    pagina = cliente.get("/metas")
    assert pagina.status_code == 200 and "Nova meta" in pagina.text
    assert 'aria-current="page"' in pagina.text
    url = f"/metas/empresas/{ids['empresa']}"
    assert _campos(
        cliente.post(
            url,
            data={
                "nome": "",
                "escopos": "3",
                "inventario_base_id": "x",
                "ano_alvo": "30",
                "reducao_percentual": "120",
            },
            headers=cabecalho,
        )
    ) == {"nome", "escopos", "inventario_base_id", "ano_alvo", "reducao_percentual"}
    assert _campos(
        cliente.post(
            url,
            data={
                "nome": "Meta 2025",
                "escopos": "1+2",
                "inventario_base_id": str(ids["inventario"]),
                "ano_alvo": "2025",
                "reducao_percentual": "40",
            },
            headers=cabecalho,
        )
    ) == {"ano_alvo"}
    criada = cliente.post(
        url,
        data={
            "nome": "Reduzir 40% até 2030",
            "escopos": "1+2",
            "inventario_base_id": str(ids["inventario"]),
            "ano_alvo": "2030",
            "reducao_percentual": "40",
        },
        headers=cabecalho,
    )
    assert criada.status_code == 204
    url_meta = criada.headers["hx-redirect"]

    pagina = cliente.get(url_meta).text
    assert "1,500 tCO₂e" in pagina and "0,900 tCO₂e" in pagina
    assert "1,260" in pagina  # trajetoria em 2027
    assert "No caminho" in pagina and "Sem inventário" in pagina
    assert "Cadastre as ações" in pagina

    # Acoes: estimativa validada; o plano compara com a reducao da meta (0,6 tCO2e).
    url_acoes = url_meta + "/acoes"
    assert _campos(
        cliente.post(
            url_acoes,
            data={"titulo": "", "reducao_estimada_tco2e": "-1", "ano_inicio": "26"},
            headers=cabecalho,
        )
    ) == {"titulo", "reducao_estimada_tco2e", "ano_inicio"}
    assert (
        _aviso(
            cliente.post(
                url_acoes,
                data={
                    "titulo": "Trocar a caldeira",
                    "reducao_estimada_tco2e": "0,2",
                    "ano_inicio": "2026",
                },
                headers=cabecalho,
            )
        )
        == "Ação cadastrada."
    )
    pagina = cliente.get(url_meta).text
    assert "não cobrem a redução da meta" in pagina and "0,200 tCO₂e/ano" in pagina
    cliente.post(
        url_acoes,
        data={"titulo": "Energia renovável", "reducao_estimada_tco2e": "0,5", "ano_inicio": "2027"},
        headers=cabecalho,
    )
    pagina = cliente.get(url_meta).text
    assert "O plano cobre a meta" in pagina
    acao = re.search(r"/metas/acoes/([0-9a-f-]{36})/situacao", pagina).group(1)  # type: ignore[union-attr]
    assert (
        _aviso(
            cliente.post(
                f"/metas/acoes/{acao}/situacao",
                data={"situacao_1": "em_andamento"},
                headers=cabecalho,
            )
        )
        == "Situação da ação atualizada."
    )
    assert "Em andamento" in cliente.get(url_meta).text

    # Arquivar nao apaga.
    meta_id = url_meta.rsplit("/", 1)[-1]
    assert (
        _aviso(cliente.post(f"/metas/{meta_id}/arquivar", headers=cabecalho)) == "Meta arquivada."
    )
    assert "Arquivada" in cliente.get("/metas").text
