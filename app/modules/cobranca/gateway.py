"""Gateway de cobranca: interface e o simulado (o Asaas esta em asaas.py).

Contrato que toda implementacao cumpre:
- criar_cobranca e idempotente pela referencia (o id da fatura): chamar de novo com a mesma
  referencia devolve a mesma cobranca, nunca cria outra. Assim, uma tarefa repetida depois de
  falha nunca cobra duas vezes.
- criar_cliente tambem e idempotente, pelo id do tenant.
- ler_evento confere a autenticidade do webhook (token) e devolve o evento normalizado; webhook
  sem token valido vira None (a rota responde 404). Evento autentico que nao e de cobranca
  volta com cobranca_id vazio (a rota responde 200 e ignora).
- exige_pagador: sem os dados do pagador, a rodada nao emite fatura nesse gateway.

O simulado nao fala com ninguem: o id da cobranca deriva da referencia e o link de pagamento
leva a uma pagina do proprio app (so fora de producao), que dispara o mesmo processamento de
um webhook de pagamento.
"""

import hmac
import json
from dataclasses import dataclass
from datetime import date
from typing import Protocol

CABECALHO_TOKEN = "X-Cobranca-Token"  # noqa: S105  # nosec B105  # nome do cabecalho, nao segredo
EVENTO_PAGO = "pagamento_confirmado"


@dataclass(frozen=True)
class Pagador:
    nome: str
    cnpj: str
    email: str


@dataclass(frozen=True)
class Cobranca:
    cobranca_id: str
    url_pagamento: str


@dataclass(frozen=True)
class Evento:
    evento_id: str
    tipo: str
    cobranca_id: str


class Gateway(Protocol):
    nome: str
    exige_pagador: bool

    async def criar_cliente(self, tenant_id: str, nome: str, pagador: Pagador | None) -> str: ...

    async def atualizar_cliente(self, cliente_id: str, pagador: Pagador) -> None: ...

    async def criar_cobranca(
        self, cliente_id: str, referencia: str, valor_centavos: int, vencimento: date,
        descricao: str,
    ) -> Cobranca: ...  # fmt: skip

    def ler_evento(self, cabecalhos: dict[str, str], corpo: bytes) -> Evento | None: ...


class GatewaySimulado:
    nome = "simulado"
    exige_pagador = False

    def __init__(self, url_base: str, token: str) -> None:
        self.url_base = url_base.rstrip("/")
        self.token = token

    async def criar_cliente(self, tenant_id: str, nome: str, pagador: Pagador | None) -> str:
        return f"sim_cli_{tenant_id}"

    async def atualizar_cliente(self, cliente_id: str, pagador: Pagador) -> None:
        return None

    async def criar_cobranca(
        self, cliente_id: str, referencia: str, valor_centavos: int, vencimento: date,
        descricao: str,
    ) -> Cobranca:  # fmt: skip
        cobranca_id = f"sim_cob_{referencia}"
        return Cobranca(cobranca_id, f"{self.url_base}/cobranca/simulador/{cobranca_id}")

    def ler_evento(self, cabecalhos: dict[str, str], corpo: bytes) -> Evento | None:
        recebido = cabecalhos.get(CABECALHO_TOKEN.lower(), "")
        if not self.token or not hmac.compare_digest(recebido.encode(), self.token.encode()):
            return None
        try:
            dados = json.loads(corpo)
            return Evento(str(dados["id"]), str(dados["tipo"]), str(dados["cobranca_id"]))
        except (ValueError, KeyError, TypeError):
            return None


def do_simulador(cobranca_id: str) -> Evento:
    """O evento que o simulador gera quando a pessoa clica em pagar."""
    return Evento(f"sim_evt_pago_{cobranca_id}", EVENTO_PAGO, cobranca_id)
