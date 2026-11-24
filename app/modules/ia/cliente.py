"""Cliente da IA (Anthropic): uma chamada com saida JSON validada por esquema.

A IA so sugere: nunca calcula emissao nem aprova dado. O modelo vem so da variavel de ambiente
ANTHROPIC_MODELO; sem ela (ou sem a chave), a IA fica desligada e as telas nao oferecem o botao.

Recusa, erro de rede, limite de taxa e erro da API viram IAIndisponivel: a tela mostra um aviso
e segue sem sugestao. O conteudo enviado e o recebido nunca vao para o log.
"""

import json
from typing import Any, Protocol

import anthropic
import structlog

from app.core.config import Configuracao

log = structlog.get_logger()

# Classificacao curta: esforco baixo basta, e o limite cobre o raciocinio adaptativo.
MAXIMO_TOKENS = 4096
BETA_FALLBACK = "server-side-fallback-2026-07-01"


class IAIndisponivel(RuntimeError):
    """A IA nao respondeu com uma sugestao valida. A tela segue sem ela."""


class ClienteIA(Protocol):
    async def gerar(
        self, sistema: str, conteudo: str, esquema: dict[str, Any]
    ) -> dict[str, Any]: ...


class ClienteAnthropic:
    def __init__(self, configuracao: Configuracao) -> None:
        if configuracao.anthropic_api_key is None or not configuracao.anthropic_modelo:
            raise ValueError("IA sem ANTHROPIC_API_KEY ou ANTHROPIC_MODELO")
        self.modelo = configuracao.anthropic_modelo
        self.fallbacks = configuracao.ia_fallbacks
        self._cliente = anthropic.AsyncAnthropic(
            api_key=configuracao.anthropic_api_key.get_secret_value(),
            timeout=configuracao.ia_tempo_limite_s,
            max_retries=2,
        )

    async def gerar(self, sistema: str, conteudo: str, esquema: dict[str, Any]) -> dict[str, Any]:
        argumentos: dict[str, Any] = {
            "model": self.modelo,
            "max_tokens": MAXIMO_TOKENS,
            "system": sistema,
            "messages": [{"role": "user", "content": conteudo}],
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": esquema},
            },
        }
        if self.fallbacks:
            # Recusa por politica: o proprio servidor repete o pedido num modelo de reserva.
            argumentos["betas"] = [BETA_FALLBACK]
            argumentos["fallbacks"] = self.fallbacks
        try:
            resposta = await self._cliente.beta.messages.create(**argumentos)
        except anthropic.RateLimitError as erro:
            log.warning("ia_falhou", motivo="limite_de_taxa")
            raise IAIndisponivel("limite de taxa") from erro
        except anthropic.APIStatusError as erro:
            log.warning("ia_falhou", motivo="status", status=erro.status_code)
            raise IAIndisponivel(f"status {erro.status_code}") from erro
        except anthropic.APIConnectionError as erro:
            log.warning("ia_falhou", motivo="conexao")
            raise IAIndisponivel("conexao") from erro
        if resposta.stop_reason == "refusal":
            log.warning("ia_falhou", motivo="recusa")
            raise IAIndisponivel("recusa")
        if resposta.stop_reason == "max_tokens":
            log.warning("ia_falhou", motivo="max_tokens")
            raise IAIndisponivel("resposta incompleta")
        texto = next((b.text for b in resposta.content if b.type == "text"), None)
        if texto is None:
            raise IAIndisponivel("sem texto")
        try:
            dados = json.loads(texto)
        except json.JSONDecodeError as erro:
            raise IAIndisponivel("json invalido") from erro
        if not isinstance(dados, dict):
            raise IAIndisponivel("json fora do esquema")
        return dados


def criar_cliente(configuracao: Configuracao) -> ClienteIA | None:
    """Cliente da IA, ou None quando ela nao esta configurada (as telas escondem o botao)."""
    if configuracao.anthropic_api_key is None or not configuracao.anthropic_modelo:
        return None
    return ClienteAnthropic(configuracao)
