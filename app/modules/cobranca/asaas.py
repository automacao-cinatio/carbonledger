"""Gateway Asaas (API v3), pela documentacao publica (docs.asaas.com).

- Autenticacao: cabecalho access_token com a chave da API; User-Agent identifica a aplicacao.
- Cliente: POST /customers com name, cpfCnpj, email e externalReference (o id do tenant).
  A API aceita cliente duplicado; por isso, antes de criar, procura pelo externalReference.
- Cobranca: POST /payments com billingType BOLETO (o boleto traz o QR Code Pix quando a conta
  tem Pix; cartao fica de fora, por decisao de produto), value, dueDate (AAAA-MM-DD),
  description e externalReference (o id da fatura). Antes de criar, procura pelo
  externalReference: tarefa repetida nunca cobra duas vezes. O link e o invoiceUrl.
- Webhook: o token configurado no Asaas chega no cabecalho asaas-access-token. O corpo traz o
  id do evento (chave de repeticao: a entrega e "at least once"), event e payment.id.
  PAYMENT_RECEIVED e PAYMENT_CONFIRMED contam como pagamento; os demais ficam registrados.
- Conciliacao: GET /payments filtrado pelo externalReference; status RECEIVED, CONFIRMED ou
  RECEIVED_IN_CASH conta como pago.
O valor vai em reais com duas casas, escrito a partir dos centavos: nada passa por float.
"""

import contextlib
import hmac
import json
from datetime import date
from typing import Any

import httpx

from app.modules.cobranca.gateway import EVENTO_PAGO, Cobranca, Evento, Pagador

URL_PRODUCAO = "https://api.asaas.com/v3"
URL_SANDBOX = "https://api-sandbox.asaas.com/v3"
CABECALHO_WEBHOOK = "asaas-access-token"
EVENTOS_DE_PAGAMENTO = frozenset({"PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"})
# Status de cobranca paga na listagem (conciliacao): os mesmos dos eventos e o recebido em
# dinheiro, que tambem quita a cobranca.
STATUS_PAGOS = frozenset({"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"})
AGENTE = "CarbonLedger"
TEMPO_LIMITE_S = 20.0


class ErroAsaas(RuntimeError):
    """Resposta inesperada do Asaas. Leva o status e os codigos de erro, nunca o corpo inteiro
    (que pode ter dado do pagador)."""


def _valor_em_reais(centavos: int) -> str:
    if centavos < 0:
        raise ValueError("valor negativo")
    return f"{centavos // 100}.{centavos % 100:02d}"


def _json_com_valor(dados: dict[str, Any], centavos: int) -> bytes:
    """JSON do corpo com "value" escrito como numero exato (sem float)."""
    base = json.dumps(dados, ensure_ascii=False)
    return f'{base[:-1]}, "value": {_valor_em_reais(centavos)}}}'.encode()


class GatewayAsaas:
    nome = "asaas"
    exige_pagador = True

    def __init__(
        self,
        url_base: str,
        chave_api: str,
        token_webhook: str,
        transporte: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url_base = url_base.rstrip("/")
        self._chave = chave_api
        self._token = token_webhook
        self._transporte = transporte  # os testes passam um MockTransport

    async def _pedir(
        self,
        metodo: str,
        caminho: str,
        *,
        params: dict[str, str] | None = None,
        corpo: bytes | None = None,
    ) -> dict[str, Any]:
        cabecalhos = {
            "access_token": self._chave,
            "User-Agent": AGENTE,
            "Accept": "application/json",
        }
        if corpo is not None:
            cabecalhos["Content-Type"] = "application/json"
        async with httpx.AsyncClient(
            base_url=self.url_base, timeout=TEMPO_LIMITE_S, transport=self._transporte
        ) as cliente:
            resposta = await cliente.request(
                metodo, caminho, params=params, content=corpo, headers=cabecalhos
            )
        if resposta.status_code // 100 != 2:
            codigos = ""
            with contextlib.suppress(ValueError, AttributeError):
                codigos = ",".join(
                    str(e.get("code")) for e in resposta.json().get("errors", []) if e
                )
            raise ErroAsaas(f"{metodo} {caminho}: HTTP {resposta.status_code} {codigos}".strip())
        dados = resposta.json()
        if not isinstance(dados, dict):
            raise ErroAsaas(f"{metodo} {caminho}: resposta fora do formato")
        return dados

    async def _primeiro(self, caminho: str, referencia: str) -> dict[str, Any] | None:
        lista = await self._pedir("GET", caminho, params={"externalReference": referencia})
        for item in lista.get("data") or []:
            if isinstance(item, dict) and not item.get("deleted"):
                return item
        return None

    @staticmethod
    def _dados_do_pagador(pagador: Pagador) -> dict[str, Any]:
        return {"name": pagador.nome, "cpfCnpj": pagador.cnpj, "email": pagador.email}

    async def criar_cliente(self, tenant_id: str, nome: str, pagador: Pagador | None) -> str:
        if pagador is None:
            raise ValueError("o Asaas exige os dados do pagador")
        existente = await self._primeiro("/customers", tenant_id)
        if existente is not None:
            cliente_id = str(existente["id"])
            await self.atualizar_cliente(cliente_id, pagador)
            return cliente_id
        dados = {**self._dados_do_pagador(pagador), "externalReference": tenant_id}
        criado = await self._pedir(
            "POST", "/customers", corpo=json.dumps(dados, ensure_ascii=False).encode()
        )
        return str(criado["id"])

    async def atualizar_cliente(self, cliente_id: str, pagador: Pagador) -> None:
        corpo = json.dumps(self._dados_do_pagador(pagador), ensure_ascii=False).encode()
        await self._pedir("PUT", f"/customers/{cliente_id}", corpo=corpo)

    async def criar_cobranca(
        self, cliente_id: str, referencia: str, valor_centavos: int, vencimento: date,
        descricao: str,
    ) -> Cobranca:  # fmt: skip
        existente = await self._primeiro("/payments", referencia)
        if existente is None:
            dados = {
                "customer": cliente_id,
                "billingType": "BOLETO",
                "dueDate": vencimento.isoformat(),
                "description": descricao[:500],
                "externalReference": referencia,
            }
            existente = await self._pedir(
                "POST", "/payments", corpo=_json_com_valor(dados, valor_centavos)
            )
        return Cobranca(str(existente["id"]), str(existente.get("invoiceUrl") or ""))

    async def cobranca_paga(self, referencia: str) -> bool:
        cobranca = await self._primeiro("/payments", referencia)
        return cobranca is not None and str(cobranca.get("status")) in STATUS_PAGOS

    def ler_evento(self, cabecalhos: dict[str, str], corpo: bytes) -> Evento | None:
        recebido = cabecalhos.get(CABECALHO_WEBHOOK, "")
        if not self._token or not hmac.compare_digest(recebido.encode(), self._token.encode()):
            return None
        try:
            dados = json.loads(corpo)
            evento_id = str(dados["id"])
            tipo = str(dados["event"])
        except (ValueError, KeyError, TypeError):
            return None
        pagamento = dados.get("payment")
        cobranca_id = str(pagamento.get("id") or "") if isinstance(pagamento, dict) else ""
        return Evento(evento_id, EVENTO_PAGO if tipo in EVENTOS_DE_PAGAMENTO else tipo, cobranca_id)
