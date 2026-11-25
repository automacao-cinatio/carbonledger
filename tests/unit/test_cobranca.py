"""Cobranca (P22) sem banco: tabela progressiva, gateway simulado, id da fatura e travas."""

import json
from datetime import date
from uuid import UUID

import pytest

from app.core.config import Configuracao
from app.modules.cobranca import precos
from app.modules.cobranca.gateway import CABECALHO_TOKEN, GatewaySimulado, do_simulador
from app.modules.cobranca.servico import competencia_de, id_da_fatura

TABELA = "3:390,00;10:290,00;*:190,00"


@pytest.mark.parametrize(
    ("empresas", "esperado"),
    [
        (0, 0),
        (1, 39000),
        (3, 117000),
        (4, 117000 + 29000),
        (10, 117000 + 7 * 29000),
        (12, 117000 + 7 * 29000 + 2 * 19000),
    ],
)
def test_calculo_progressivo(empresas: int, esperado: int) -> None:
    valor, linhas = precos.calcular(precos.ler_tabela(TABELA), empresas)
    assert valor == esperado
    assert sum(linha.empresas for linha in linhas) == empresas
    assert sum(linha.subtotal_centavos for linha in linhas) == valor


def test_detalhamento_mostra_as_faixas_usadas() -> None:
    _valor, linhas = precos.calcular(precos.ler_tabela(TABELA), 12)
    assert [(linha.de, linha.ate, linha.preco_centavos) for linha in linhas] == [
        (1, 3, 39000),
        (4, 10, 29000),
        (11, 12, 19000),
    ]


def test_preco_unico_e_milhar() -> None:
    assert precos.calcular(precos.ler_tabela("*:1.250,50"), 2)[0] == 250100


@pytest.mark.parametrize(
    ("texto", "trecho"),
    [
        ("", "ultima faixa"),
        ("3:390,00", "ultima faixa"),
        ("3:390,00;*:x", "preco invalido"),
        ("10:1,00;3:2,00;*:3,00", "crescentes"),
        ("0:1,00;*:2,00", "limite invalido"),
        ("3=1,00;*:2,00", "sem ':'"),
        ("*:1,005", "ate centavos"),
        ("*:-1,00", "zero ou mais"),
    ],
)
def test_tabela_invalida(texto: str, trecho: str) -> None:
    with pytest.raises(precos.TabelaInvalida, match=trecho):
        precos.ler_tabela(texto)


async def test_gateway_simulado_e_idempotente_e_confere_o_token() -> None:
    gateway = GatewaySimulado("http://localhost:8000/", "segredo")
    primeira = await gateway.criar_cobranca("cli", "ref-1", 100, date(2026, 1, 10), "x")
    segunda = await gateway.criar_cobranca("cli", "ref-1", 100, date(2026, 1, 10), "x")
    assert primeira == segunda
    assert (
        primeira.url_pagamento == f"http://localhost:8000/cobranca/simulador/{primeira.cobranca_id}"
    )
    corpo = json.dumps({"id": "e1", "tipo": "pagamento_confirmado", "cobranca_id": "c1"}).encode()
    assert gateway.ler_evento({CABECALHO_TOKEN.lower(): "segredo"}, corpo) is not None
    assert gateway.ler_evento({CABECALHO_TOKEN.lower(): "errado"}, corpo) is None
    assert gateway.ler_evento({}, corpo) is None
    assert gateway.ler_evento({CABECALHO_TOKEN.lower(): "segredo"}, b"{") is None
    assert do_simulador("c1").cobranca_id == "c1"


def test_id_da_fatura_e_estavel_por_tenant_e_mes() -> None:
    tenant = UUID("aaaaaaaa-0000-4000-8000-000000000001")
    janeiro = competencia_de(date(2026, 1, 20))
    assert janeiro == date(2026, 1, 1)
    assert id_da_fatura(tenant, janeiro) == id_da_fatura(tenant, date(2026, 1, 1))
    assert id_da_fatura(tenant, janeiro) != id_da_fatura(tenant, date(2026, 2, 1))


def test_travas_da_cobranca() -> None:
    with pytest.raises(ValueError, match="COBRANCA_WEBHOOK_TOKEN"):
        Configuracao(cobranca_tabela=TABELA, cobranca_webhook_token=None)
    with pytest.raises(ValueError, match="simulado ou asaas"):
        Configuracao(cobranca_gateway="boleto")
    with pytest.raises(ValueError, match="gateway simulado"):
        Configuracao(
            ambiente="producao",
            provedor_chaves="kms",
            kms_chave_id="alias/x",
            chaves_locais=None,
            evidencias_modo_retencao="COMPLIANCE",
            app_url_base="https://carbonledger.example",
            oidc_dev_emissor=None,
            database_url="postgresql://u:p@banco/cl?sslmode=require",
            cobranca_tabela=TABELA,
            cobranca_webhook_token="t",  # noqa: S106
        )
