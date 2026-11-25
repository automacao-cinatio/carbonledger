"""Gateway Asaas contra um servidor falso (httpx.MockTransport): cabecalhos, corpo exato,
idempotencia por externalReference, erros sem dado do pagador, webhook e travas."""

import json
from datetime import date
from typing import Any

import httpx
import pytest

from app.core.config import Configuracao
from app.modules.cobranca.asaas import (
    CABECALHO_WEBHOOK,
    URL_PRODUCAO,
    URL_SANDBOX,
    ErroAsaas,
    GatewayAsaas,
)
from app.modules.cobranca.gateway import EVENTO_PAGO, Pagador

PAGADOR = Pagador("Controladora Exemplo S.A.", "11222333000181", "financeiro@exemplo.example")
TENANT = "aaaaaaaa-0000-4000-8000-000000000001"
FATURA = "bbbbbbbb-0000-4000-8000-000000000002"
TABELA = "3:390,00;*:190,00"
CHAVE_TESTE = "$aact_hmlg_chave"  # nosec B105  # chave falsa de teste


class AsaasFalso:
    """Guarda os pedidos e responde como a API v3, com clientes e cobrancas em memoria."""

    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.clientes: list[dict[str, Any]] = []
        self.cobrancas: list[dict[str, Any]] = []
        self.falhar_com: tuple[int, dict[str, Any]] | None = None

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        if self.falhar_com is not None:
            return httpx.Response(self.falhar_com[0], json=self.falhar_com[1])
        caminho = pedido.url.path.removeprefix("/v3")
        referencia = pedido.url.params.get("externalReference")
        if pedido.method == "GET" and caminho in ("/customers", "/payments"):
            itens = self.clientes if caminho == "/customers" else self.cobrancas
            achados = [i for i in itens if i.get("externalReference") == referencia]
            return httpx.Response(200, json={"object": "list", "hasMore": False, "data": achados})
        corpo = json.loads(pedido.content) if pedido.content else {}
        if pedido.method == "POST" and caminho == "/customers":
            novo = {"id": f"cus_{len(self.clientes) + 1:06d}", **corpo}
            self.clientes.append(novo)
            return httpx.Response(200, json=novo)
        if pedido.method == "PUT" and caminho.startswith("/customers/"):
            return httpx.Response(200, json={"id": caminho.rsplit("/", 1)[1], **corpo})
        if pedido.method == "POST" and caminho == "/payments":
            nova = {
                "id": f"pay_{len(self.cobrancas) + 1:06d}",
                "status": "PENDING",
                "deleted": False,
                "invoiceUrl": f"https://sandbox.asaas.example/i/{len(self.cobrancas) + 1}",
                **corpo,
            }
            self.cobrancas.append(nova)
            return httpx.Response(200, json=nova)
        return httpx.Response(404, json={"errors": [{"code": "not_found"}]})


def _gateway(falso: AsaasFalso) -> GatewayAsaas:
    return GatewayAsaas(
        URL_SANDBOX, CHAVE_TESTE, "token-do-webhook", transporte=httpx.MockTransport(falso)
    )


async def test_cliente_criado_uma_vez_e_atualizado() -> None:
    falso = AsaasFalso()
    gateway = _gateway(falso)
    cliente = await gateway.criar_cliente(TENANT, "Cliente", PAGADOR)
    primeiro = falso.pedidos[0]
    assert primeiro.headers["access_token"] == CHAVE_TESTE
    assert primeiro.headers["user-agent"] == "CarbonLedger"
    assert str(primeiro.url).startswith("https://api-sandbox.asaas.com/v3/customers?")
    criado = json.loads(falso.pedidos[1].content)
    assert criado == {
        "name": PAGADOR.nome,
        "cpfCnpj": PAGADOR.cnpj,
        "email": PAGADOR.email,
        "externalReference": TENANT,
    }

    # De novo (tarefa repetida depois de falha): acha pelo externalReference, nao duplica.
    falso.pedidos.clear()
    assert await gateway.criar_cliente(TENANT, "Cliente", PAGADOR) == cliente
    assert [p.method for p in falso.pedidos] == ["GET", "PUT"]
    assert len(falso.clientes) == 1

    # Pagador novo: PUT no mesmo cliente.
    falso.pedidos.clear()
    outro = Pagador("Outra S.A.", "11444777000161", "contas@outra.example")
    await gateway.atualizar_cliente(cliente, outro)
    assert falso.pedidos[0].method == "PUT"
    assert falso.pedidos[0].url.path == f"/v3/customers/{cliente}"
    assert json.loads(falso.pedidos[0].content)["cpfCnpj"] == "11444777000161"


async def test_asaas_exige_o_pagador() -> None:
    with pytest.raises(ValueError, match="pagador"):
        await _gateway(AsaasFalso()).criar_cliente(TENANT, "Cliente", None)


async def test_cobranca_boleto_com_valor_exato_e_idempotente() -> None:
    falso = AsaasFalso()
    gateway = _gateway(falso)
    cobranca = await gateway.criar_cobranca(
        "cus_000001", FATURA, 123456, date(2026, 12, 10), "CarbonLedger 12/2026"
    )
    corpo_texto = falso.pedidos[1].content.decode()
    # Valor escrito a partir dos centavos, sem float: 1234.56 exato.
    assert '"value": 1234.56' in corpo_texto
    corpo = json.loads(corpo_texto)
    assert corpo["billingType"] == "BOLETO"
    assert corpo["dueDate"] == "2026-12-10" and corpo["customer"] == "cus_000001"
    assert corpo["externalReference"] == FATURA
    assert cobranca.cobranca_id == "pay_000001"
    assert cobranca.url_pagamento == "https://sandbox.asaas.example/i/1"

    falso.pedidos.clear()
    de_novo = await gateway.criar_cobranca(
        "cus_000001", FATURA, 123456, date(2026, 12, 10), "CarbonLedger 12/2026"
    )
    assert de_novo == cobranca and [p.method for p in falso.pedidos] == ["GET"]

    # Cobranca removida no Asaas nao conta: cria outra.
    falso.cobrancas[0]["deleted"] = True
    nova = await gateway.criar_cobranca(
        "cus_000001", FATURA, 123456, date(2026, 12, 10), "CarbonLedger 12/2026"
    )
    assert nova.cobranca_id == "pay_000002"


@pytest.mark.parametrize(("centavos", "texto"), [(0, "0.00"), (5, "0.05"), (39000, "390.00")])
async def test_valores_pequenos_e_redondos(centavos: int, texto: str) -> None:
    falso = AsaasFalso()
    await _gateway(falso).criar_cobranca("cus_1", FATURA, centavos, date(2026, 1, 1), "x")
    assert f'"value": {texto}' in falso.pedidos[1].content.decode()


async def test_erro_do_asaas_sem_dado_do_pagador() -> None:
    falso = AsaasFalso()
    falso.falhar_com = (
        400,
        {"errors": [{"code": "invalid_cpfCnpj", "description": f"CNPJ {PAGADOR.cnpj} invalido"}]},
    )
    with pytest.raises(ErroAsaas) as erro:
        await _gateway(falso).criar_cliente(TENANT, "Cliente", PAGADOR)
    assert "HTTP 400" in str(erro.value) and "invalid_cpfCnpj" in str(erro.value)
    assert PAGADOR.cnpj not in str(erro.value)


def _evento(evento: str, pagamento: dict[str, Any] | None = None) -> bytes:
    dados: dict[str, Any] = {"id": "evt_1", "event": evento, "dateCreated": "2026-12-01"}
    if pagamento is not None:
        dados["payment"] = pagamento
    return json.dumps(dados).encode()


def test_webhook_confere_o_token_e_normaliza() -> None:
    gateway = _gateway(AsaasFalso())
    certo = {CABECALHO_WEBHOOK: "token-do-webhook"}
    recebido = gateway.ler_evento(certo, _evento("PAYMENT_RECEIVED", {"id": "pay_1"}))
    assert recebido is not None
    assert (recebido.evento_id, recebido.tipo, recebido.cobranca_id) == (
        "evt_1",
        EVENTO_PAGO,
        "pay_1",
    )
    confirmado = gateway.ler_evento(certo, _evento("PAYMENT_CONFIRMED", {"id": "pay_1"}))
    assert confirmado is not None and confirmado.tipo == EVENTO_PAGO
    vencido = gateway.ler_evento(certo, _evento("PAYMENT_OVERDUE", {"id": "pay_1"}))
    assert vencido is not None and vencido.tipo == "PAYMENT_OVERDUE"
    # Evento sem cobranca: autentico, mas sem cobranca_id (a rota responde 200 e ignora).
    sem_cobranca = gateway.ler_evento(certo, _evento("ACCOUNT_STATUS_UPDATED"))
    assert sem_cobranca is not None and sem_cobranca.cobranca_id == ""
    # Token errado, ausente ou corpo quebrado: None (404).
    assert gateway.ler_evento({CABECALHO_WEBHOOK: "outro"}, _evento("PAYMENT_RECEIVED")) is None
    assert gateway.ler_evento({}, _evento("PAYMENT_RECEIVED")) is None
    assert gateway.ler_evento(certo, b"{") is None


def test_travas_do_asaas() -> None:
    base = {"cobranca_tabela": TABELA, "cobranca_webhook_token": "t", "cobranca_gateway": "asaas"}
    with pytest.raises(ValueError, match="ASAAS_API_KEY"):
        Configuracao(**base)
    with pytest.raises(ValueError, match="ASAAS_API_KEY"):
        Configuracao(**base, asaas_api_key="")
    # Fora de producao, nunca a URL de producao (nada de cobranca real a partir de dev).
    with pytest.raises(ValueError, match="URL de producao"):
        Configuracao(**base, asaas_api_key="k", asaas_base_url=URL_PRODUCAO)
    with pytest.raises(ValueError, match="https"):
        Configuracao(**base, asaas_api_key="k", asaas_base_url="http://api-sandbox.asaas.com/v3")
    configuracao = Configuracao(**base, asaas_api_key="k")
    assert configuracao.asaas_url == URL_SANDBOX
    producao = {
        "ambiente": "producao",
        "provedor_chaves": "kms",
        "kms_chave_id": "alias/x",
        "chaves_locais": None,
        "evidencias_modo_retencao": "COMPLIANCE",
        "app_url_base": "https://carbonledger.example",
        "oidc_dev_emissor": None,
        "database_url": "postgresql://u:p@banco/cl?sslmode=require",
    }
    with pytest.raises(ValueError, match="URL de producao"):
        Configuracao(**base, **producao, asaas_api_key="k")
    assert Configuracao(
        **base, **producao, asaas_api_key="k", asaas_base_url=URL_PRODUCAO
    ).em_producao
