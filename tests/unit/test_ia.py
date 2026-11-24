"""IA sem rede: o cliente monta o pedido certo e trata recusa e erro; as sugestoes so escolhem
entre as opcoes que existem."""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2 as httpx
import pytest

from app.core.config import Configuracao
from app.modules.ia.cliente import ClienteAnthropic, IAIndisponivel, criar_cliente
from app.modules.ia.sugestoes import opinar_sobre_dado, sugerir_fonte

BASE = {
    "database_url": "postgresql://a:b@h/d",
    "redis_url": "redis://h",
    "s3_endpoint": "http://h",
    "s3_access_key": "x",
    "s3_secret_key": "y",
}


def _configuracao(**valores: Any) -> Configuracao:
    return Configuracao(**{**BASE, **valores})


class _Mensagens:
    def __init__(self, resposta: Any = None, erro: Exception | None = None) -> None:
        self.resposta = resposta
        self.erro = erro
        self.pedidos: list[dict[str, Any]] = []

    async def create(self, **argumentos: Any) -> Any:
        self.pedidos.append(argumentos)
        if self.erro is not None:
            raise self.erro
        return self.resposta


def _cliente(mensagens: _Mensagens, **config: Any) -> ClienteAnthropic:
    cliente = ClienteAnthropic(
        _configuracao(anthropic_api_key="chave-de-teste", anthropic_modelo="modelo-teste", **config)
    )
    cliente._cliente = SimpleNamespace(beta=SimpleNamespace(messages=mensagens))  # type: ignore[assignment]
    return cliente


def _resposta(dados: Any, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=json.dumps(dados)),
        ],
    )


def test_sem_chave_ou_modelo_a_ia_fica_desligada() -> None:
    assert criar_cliente(_configuracao()) is None
    assert criar_cliente(_configuracao(anthropic_api_key="x")) is None
    assert criar_cliente(_configuracao(anthropic_modelo="m")) is None
    assert criar_cliente(_configuracao(anthropic_api_key="x", anthropic_modelo="m")) is not None


async def test_pedido_usa_o_modelo_da_configuracao_e_esquema() -> None:
    mensagens = _Mensagens(_resposta({"ok": True}))
    esquema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert await _cliente(mensagens).gerar("sistema", "conteudo", esquema) == {"ok": True}
    (pedido,) = mensagens.pedidos
    assert pedido["model"] == "modelo-teste"
    assert pedido["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": esquema},
    }
    assert pedido["fallbacks"] == "default" and pedido["betas"] == [
        "server-side-fallback-2026-07-01"
    ]
    assert "temperature" not in pedido and "thinking" not in pedido


async def test_fallbacks_podem_ser_desligados() -> None:
    mensagens = _Mensagens(_resposta({}))
    await _cliente(mensagens, ia_fallbacks="").gerar("s", "c", {})
    assert "fallbacks" not in mensagens.pedidos[0] and "betas" not in mensagens.pedidos[0]


_REQUISICAO = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "mensagens",
    [
        _Mensagens(_resposta({}, stop_reason="refusal")),
        _Mensagens(_resposta({}, stop_reason="max_tokens")),
        _Mensagens(SimpleNamespace(stop_reason="end_turn", content=[])),
        _Mensagens(
            SimpleNamespace(
                stop_reason="end_turn", content=[SimpleNamespace(type="text", text="{quebrado")]
            )
        ),
        _Mensagens(erro=anthropic.APIConnectionError(request=_REQUISICAO)),
        _Mensagens(
            erro=anthropic.RateLimitError(
                "limite", response=httpx.Response(429, request=_REQUISICAO), body=None
            )
        ),
        _Mensagens(
            erro=anthropic.InternalServerError(
                "falha", response=httpx.Response(500, request=_REQUISICAO), body=None
            )
        ),
    ],
)
async def test_recusa_e_erros_viram_ia_indisponivel(mensagens: _Mensagens) -> None:
    with pytest.raises(IAIndisponivel):
        await _cliente(mensagens).gerar("s", "c", {})


class _IAFixa:
    def __init__(self, dados: dict[str, Any]) -> None:
        self.dados = dados
        self.chamadas: list[tuple[str, str, dict[str, Any]]] = []

    async def gerar(self, sistema: str, conteudo: str, esquema: dict[str, Any]) -> dict[str, Any]:
        self.chamadas.append((sistema, conteudo, esquema))
        return self.dados


CATEGORIAS = {
    "escopo1.combustao_movel": "Escopo 1: Combustão móvel",
    "escopo2.energia_eletrica": "x",
}
ATIVIDADES = {"teste.diesel": "Diesel (teste)"}


async def test_sugestao_so_escolhe_entre_as_opcoes() -> None:
    ia = _IAFixa(
        {
            "categoria": "escopo1.combustao_movel",
            "atividade": "teste.diesel",
            "confianca": "alta",
            "justificativa": "Caminhões queimam diesel.",
        }
    )
    sugestao = await sugerir_fonte(
        ia,
        descricao="Frota de caminhões",
        unidade_medida="L",
        categorias=CATEGORIAS,
        atividades=ATIVIDADES,
    )
    assert sugestao.categoria == "escopo1.combustao_movel" and sugestao.atividade == "teste.diesel"
    _sistema, conteudo, esquema = ia.chamadas[0]
    assert esquema["properties"]["categoria"]["enum"] == sorted(CATEGORIAS)
    assert esquema["properties"]["atividade"]["enum"] == ["", "teste.diesel"]
    assert "Frota de caminhões" in conteudo and "L" in conteudo


@pytest.mark.parametrize(
    "dados",
    [
        {"categoria": "inventada", "atividade": "", "confianca": "alta", "justificativa": "x"},
        {"categoria": "escopo1.combustao_movel", "atividade": "outra", "confianca": "alta",
         "justificativa": "x"},
        {"categoria": "escopo1.combustao_movel", "atividade": "", "confianca": "total",
         "justificativa": "x"},
        {"categoria": 3},
    ],
)  # fmt: skip
async def test_sugestao_fora_da_lista_e_descartada(dados: dict[str, Any]) -> None:
    with pytest.raises(IAIndisponivel):
        await sugerir_fonte(
            _IAFixa(dados),
            descricao="x",
            unidade_medida="L",
            categorias=CATEGORIAS,
            atividades=ATIVIDADES,
        )


async def test_opiniao_sobre_o_dado() -> None:
    ia = _IAFixa({"fora_do_padrao": True, "explicacao": "Dez vezes acima dos outros meses."})
    opiniao = await opinar_sobre_dado(
        ia,
        descricao="Caldeira",
        unidade="L",
        historico=[("Janeiro", Decimal("1000")), ("Fevereiro", Decimal("1100"))],
        novo=("Março", Decimal("10500")),
    )
    assert opiniao.fora_do_padrao and "Dez vezes" in opiniao.explicacao
    conteudo = ia.chamadas[0][1]
    assert "Janeiro: 1000 L" in conteudo and "Março: 10500 L" in conteudo
    with pytest.raises(IAIndisponivel):
        await opinar_sobre_dado(
            _IAFixa({"fora_do_padrao": "talvez", "explicacao": ""}),
            descricao="x",
            unidade="L",
            historico=[],
            novo=("Março", Decimal(1)),
        )
